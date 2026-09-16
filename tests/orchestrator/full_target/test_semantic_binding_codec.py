# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""P1.1 red tests: HTN codecs, nominal identities and the plan schema fixtures.

Covers the three things §6.4 / TG decision 7 and §18.3 say must not be possible:
a goal id claimed by an id prefix, a ``PlanProposal`` passed off as a compiled
``ProposedPlanDelta``, and a model-authored method carrying its own ADMITTED.
"""

from __future__ import annotations

from typing import Any

import pytest
from full_target_world import (
    FIXTURE_ROOT,
    HASH_A,
    HASH_B,
    UPSTREAM_ROOT,
    World,
    goal_signature,
    load_plan_fixtures,
    method_contract,
    sha256_of,
    source_manifest,
    task_binding,
    tref,
    upstream_plan_pack_available,
    vref,
)

from agent_orchestrator.contracts.evidence_state import (
    PreconditionPhase,
    PreconditionWitnessRecord,
    TruthValue,
    phase_check_points,
)
from agent_orchestrator.contracts.htn import (
    MAX_CONDITION_NODES,
    Binding,
    BoundInput,
    BudgetInheritance,
    ChildBinding,
    ContractRevision,
    DataRequirement,
    DispatchGeneration,
    EndpointKind,
    ExecutionFeedbackV1,
    GoalSignature,
    GraphStructureBudget,
    MethodContract,
    MethodInstanceDraft,
    MethodRegistration,
    MethodRegistryStatus,
    MethodStep,
    NetworkEndpoint,
    ObligationCoverage,
    ObligationId,
    ObligationOpening,
    ObligationRelation,
    OccurrenceId,
    OccurrenceSpec,
    OrderConstraint,
    PlanProposal,
    PlanRevision,
    PortCardinality,
    PortOrdering,
    PortSpec,
    PreconditionRef,
    ProposedPlanDelta,
    ReadItem,
    ReadItemKind,
    RegistryAuthor,
    RelationKind,
    ReleaseCondition,
    Requiredness,
    ResourceRef,
    ReusePolicy,
    ScopeEpochRead,
    SemanticReadSet,
    SideEffectKind,
    StructureBudget,
    SupportSetRead,
    TaskForm,
    TaskRef,
    TaskSemanticBindingV1,
    TypedEdge,
    admit_method,
    assert_method_instances_match_occurrences,
    assert_occurrences_match_bindings,
    condition_digest,
    require_commit_ready,
    resource_conflicts,
    undeclared_set_ports,
)
from agent_orchestrator.contracts.models import ContractError
from agent_orchestrator.contracts.resolution import GoalResolution
from agent_orchestrator.contracts.semantic_base import (
    MAX_ID,
    MAX_LIST,
    MAX_TEXT,
    Provenance,
    TypedRefKind,
    VersionedRef,
)


def read_set() -> SemanticReadSet:
    return SemanticReadSet(
        requirements_revision=3,
        goal_revisions=(
            ReadItem(kind=ReadItemKind.TASK, id="task-1", semantic_revision=2, content_hash=HASH_A),
        ),
        method_revisions=(
            ReadItem(
                kind=ReadItemKind.METHOD, id="method-a", semantic_revision=1, content_hash=HASH_A
            ),
        ),
        observation_revisions=(
            ReadItem(kind=ReadItemKind.FACT, id="fact-1", semantic_revision=7, content_hash=HASH_A),
        ),
        acceptance_revisions=(
            ReadItem(
                kind=ReadItemKind.ACCEPTANCE,
                id="acceptance-1",
                semantic_revision=1,
                content_hash=HASH_A,
            ),
        ),
        manager_epoch=4,
        budget_grant_revision=2,
        support_sets=(
            SupportSetRead(support_set_id="support-1", revision=5, member_digest=HASH_B),
        ),
        scope_epochs=(ScopeEpochRead(scope_id="mission-1", validity_epoch=9),),
    )


def method_instance_draft() -> MethodInstanceDraft:
    contract = method_contract()
    return MethodInstanceDraft(
        instance_id="instance-1",  # type: ignore[arg-type]
        goal_id=TaskRef("task-1"),
        obligation_id=ObligationId("obligation-1"),
        method_ref=contract.method_ref(),
        grounded_parameters=(Binding(name="subject", value="alpha"),),
        world_snapshot_id="snapshot-1",
        precondition_witnesses=(
            PreconditionWitnessRecord(
                condition_digest=HASH_A,
                phase=PreconditionPhase.SELECT,
                truth=TruthValue.TRUE,
                witness_ref=tref(TypedRefKind.OBSERVATION, "witness-1"),
            ),
        ),
        child_bindings=(
            ChildBinding(
                instance_id="instance-1",  # type: ignore[arg-type]
                slot_key="extract",
                occurrence_id=OccurrenceId("occurrence-1"),
                obligation_id=ObligationId("obligation-2"),
                requiredness=Requiredness.REQUIRED,
                reuse_policy=ReusePolicy.REUSE_ACCEPTED,
            ),
        ),
        plan_revision=PlanRevision(4),
    )


# --------------------------------------------------------------------------------------
# Round trips and hash stability
# --------------------------------------------------------------------------------------


def test_task_semantic_binding_round_trips_and_hashes_stably() -> None:
    binding = TaskSemanticBindingV1(
        task_id=TaskRef("task-1"),
        obligation_id=ObligationId("obligation-1"),
        contract_revision=ContractRevision(2),
        contract_hash=HASH_B,
        form=TaskForm.PRIMITIVE,
        goal_signature=goal_signature(),
        typed_parameters={"subject": "alpha", "depth": 3},
        requirement_refs=("c-complete",),
        input_ports=(
            PortSpec(
                port_key="sources", schema_ref=vref("source-list"), cardinality=PortCardinality.SET
            ),
        ),
        output_ports=(PortSpec(port_key="report", schema_ref=vref("report")),),
        operator_ref=vref("operator-1"),
        capability_requirements=("sources.read",),
        precondition_refs=(
            PreconditionRef(condition_digest=HASH_A, phase=PreconditionPhase.ACCEPT),
        ),
        input_binding_revision=3,  # type: ignore[arg-type]
        dispatch_generation=DispatchGeneration(5),
    )

    restored = TaskSemanticBindingV1.from_json(binding.to_json())

    assert restored == binding
    assert restored.to_json() == binding.to_json()
    assert restored.content_hash() == binding.content_hash()
    assert len(binding.content_hash()) == 64


def test_the_binding_hash_moves_when_a_semantic_field_moves() -> None:
    before = task_binding()
    after = task_binding(parameters={"subject": "beta"})
    assert before.content_hash() != after.content_hash()


def test_form_and_operator_ref_agree() -> None:
    with pytest.raises(ContractError, match="primitive task binding needs an operator_ref"):
        TaskSemanticBindingV1(
            task_id=TaskRef("task-2"),
            obligation_id=ObligationId("obligation-1"),
            contract_revision=ContractRevision(1),
            contract_hash=HASH_B,
            form=TaskForm.PRIMITIVE,
            goal_signature=goal_signature(),
        )
    with pytest.raises(ContractError, match="must not carry an operator_ref"):
        TaskSemanticBindingV1(
            task_id=TaskRef("task-3"),
            obligation_id=ObligationId("obligation-1"),
            contract_revision=ContractRevision(1),
            contract_hash=HASH_B,
            form=TaskForm.COMPOUND,
            goal_signature=goal_signature(),
            operator_ref=vref("operator-1"),
        )


def test_method_contract_round_trips_and_its_ref_is_content_addressed() -> None:
    world = World({"ready": TruthValue.TRUE})
    contract = method_contract(applicable_when=(world.atom("ready"),))

    restored = MethodContract.from_json(contract.to_json())

    assert restored.to_json() == contract.to_json()
    assert restored.method_ref() == contract.method_ref()
    other = method_contract(method_id="compare-two-sources-v2")
    assert other.method_ref().content_hash != contract.method_ref().content_hash


def test_method_instance_draft_round_trips() -> None:
    draft = method_instance_draft()
    restored = MethodInstanceDraft.from_json(draft.to_json())
    assert restored == draft
    assert restored.parameters_digest() == draft.parameters_digest()


def test_semantic_read_set_round_trips_with_its_support_member_digest() -> None:
    original = read_set()
    restored = SemanticReadSet.from_json(original.to_json())
    assert restored == original
    assert restored.support_sets[0].member_digest == HASH_B


def test_goal_signature_round_trips() -> None:
    signature = goal_signature()
    assert GoalSignature.from_json(signature.to_json()) == signature


# --------------------------------------------------------------------------------------
# Nominal identity: a prefix buys nothing (TG §3.1, decision 7)
# --------------------------------------------------------------------------------------


def test_goal_id_must_arrive_as_a_typed_task_reference() -> None:
    payload = method_instance_draft().to_json()
    payload["goal_id"] = "task:obligation-1"

    with pytest.raises(ContractError, match="id prefix does not establish the kind"):
        MethodInstanceDraft.from_json(payload)


def test_goal_id_refuses_a_reference_of_another_kind() -> None:
    payload = method_instance_draft().to_json()
    payload["goal_id"] = tref(TypedRefKind.OBSERVATION, "obligation-1").to_json()

    with pytest.raises(ContractError, match="kind must be task"):
        MethodInstanceDraft.from_json(payload)


def test_goal_id_and_obligation_id_are_separate_fields() -> None:
    draft = method_instance_draft()
    assert str(draft.goal_id) == "task-1"
    assert str(draft.obligation_id) == "obligation-1"
    assert draft.to_json()["goal_id"]["kind"] == "task"
    assert draft.to_json()["obligation_id"] == "obligation-1"


# --------------------------------------------------------------------------------------
# PlanProposal is not a ProposedPlanDelta (§18.3)
# --------------------------------------------------------------------------------------


def plan_proposal() -> PlanProposal:
    return PlanProposal.from_json(
        {
            "schema_version": 1,
            "proposal_id": "proposal-1",
            "mission_id": "mission-1",
            "expected_plan_revision": 2,
            "trigger_refs": [],
            "read_set": [
                {
                    "kind": "obligation",
                    "id": "obligation-1",
                    "semantic_revision": 1,
                    "content_hash": HASH_A,
                }
            ],
            "operations": [
                {
                    "op": "refine",
                    "goal_id": "task-1",
                    "obligation_id": "obligation-1",
                    "method_ref": vref("method-a").to_json(),
                    "bindings": {"subject": "alpha"},
                }
            ],
            "rationale": "refine with a registered method and keep the duty",
            "running_work_policy": "retain_if_bindings_unchanged",
        }
    )


def proposed_delta() -> ProposedPlanDelta:
    return ProposedPlanDelta(
        delta_id="delta-1",
        mission_id="mission-1",  # type: ignore[arg-type]
        base_plan_revision=PlanRevision(2),
        read_set=read_set(),
        method_instances=(method_instance_draft(),),
        occurrences=(
            OccurrenceSpec(
                occurrence_id=OccurrenceId("occurrence-1"),
                task_id=TaskRef("task-2"),
                obligation_id=ObligationId("obligation-2"),
                form=TaskForm.PRIMITIVE,
            ),
            OccurrenceSpec(
                occurrence_id=OccurrenceId("occurrence-2"),
                task_id=TaskRef("task-3"),
                obligation_id=ObligationId("obligation-3"),
                form=TaskForm.PRIMITIVE,
            ),
        ),
        order_constraints=(
            OrderConstraint(
                before=OccurrenceId("occurrence-1"),
                after=OccurrenceId("occurrence-2"),
                release_condition=ReleaseCondition.ACCEPTED,
            ),
        ),
        data_requirements=(
            DataRequirement(
                requirement_id="requirement-1",
                producer_occurrence=OccurrenceId("occurrence-1"),
                output_port="evidence",
                consumer_occurrence=OccurrenceId("occurrence-2"),
                input_port="evidence",
                schema_ref=vref("evidence"),
                assurance_policy_ref="assurance-default",
                freshness_policy_ref="freshness-default",
            ),
        ),
        obligation_coverage=(
            ObligationCoverage(
                obligation_id=ObligationId("obligation-1"),
                criterion_ids=("c-complete",),
                covered_by=(OccurrenceId("occurrence-2"),),
            ),
        ),
        compiled_from_proposal_id="proposal-1",
    )


def test_a_plan_proposal_cannot_be_committed() -> None:
    with pytest.raises(ContractError, match="compile it into a ProposedPlanDelta"):
        require_commit_ready(plan_proposal())


def test_a_compiled_delta_is_what_a_commit_accepts() -> None:
    delta = proposed_delta()
    assert require_commit_ready(delta) is delta


def test_the_two_shapes_do_not_decode_from_each_other() -> None:
    with pytest.raises(ContractError):
        ProposedPlanDelta.from_json(plan_proposal().to_json())
    with pytest.raises(ContractError):
        PlanProposal.from_json(proposed_delta().to_json())


def test_both_shapes_round_trip_on_their_own_payloads() -> None:
    proposal = plan_proposal()
    delta = proposed_delta()
    assert PlanProposal.from_json(proposal.to_json()).to_json() == proposal.to_json()
    assert ProposedPlanDelta.from_json(delta.to_json()) == delta


def test_a_delta_refuses_an_edge_to_an_occurrence_it_never_names() -> None:
    with pytest.raises(ContractError, match="unknown occurrence"):
        ProposedPlanDelta(
            delta_id="delta-2",
            mission_id="mission-1",  # type: ignore[arg-type]
            base_plan_revision=PlanRevision(1),
            read_set=read_set(),
            order_constraints=(
                OrderConstraint(
                    before=OccurrenceId("occurrence-x"), after=OccurrenceId("occurrence-y")
                ),
            ),
        )


def test_a_single_valued_input_port_takes_one_binding() -> None:
    delta = proposed_delta()
    doubled = delta.data_requirements + (
        DataRequirement(
            requirement_id="requirement-2",
            producer_occurrence=OccurrenceId("occurrence-1"),
            output_port="other",
            consumer_occurrence=OccurrenceId("occurrence-2"),
            input_port="evidence",
            schema_ref=vref("evidence"),
            assurance_policy_ref="assurance-default",
            freshness_policy_ref="freshness-default",
        ),
    )
    with pytest.raises(ContractError, match="binds one twice"):
        ProposedPlanDelta(
            delta_id="delta-3",
            mission_id="mission-1",  # type: ignore[arg-type]
            base_plan_revision=PlanRevision(2),
            read_set=read_set(),
            occurrences=delta.occurrences,
            data_requirements=doubled,
        )


# --------------------------------------------------------------------------------------
# Registry status, precondition phases, condition digests
# --------------------------------------------------------------------------------------


def test_a_model_authored_method_may_only_be_submitted_as_draft() -> None:
    ref = method_contract().method_ref()

    with pytest.raises(ContractError, match="registry_status past DRAFT"):
        admit_method(ref, MethodRegistryStatus.ADMITTED, author=RegistryAuthor.MODEL)

    draft = admit_method(ref, MethodRegistryStatus.DRAFT, author=RegistryAuthor.MODEL)
    assert draft.status is MethodRegistryStatus.DRAFT

    admitted = admit_method(ref, MethodRegistryStatus.ADMITTED, author=RegistryAuthor.SYSTEM)
    assert admitted.status is MethodRegistryStatus.ADMITTED


def test_a_trial_admission_is_scoped_to_one_mission() -> None:
    ref = method_contract().method_ref()
    with pytest.raises(ContractError, match="scoped to one mission"):
        admit_method(ref, MethodRegistryStatus.TRIAL_ADMITTED, author=RegistryAuthor.SYSTEM)
    scoped = admit_method(
        ref,
        MethodRegistryStatus.TRIAL_ADMITTED,
        author=RegistryAuthor.SYSTEM,
        trial_scope_mission="mission-1",
    )
    assert scoped.trial_scope_mission == "mission-1"


def test_an_undeclared_precondition_phase_defaults_to_select_and_rechecks_at_accept() -> None:
    assert phase_check_points(None) == (PreconditionPhase.SELECT, PreconditionPhase.ACCEPT)
    assert phase_check_points(PreconditionPhase.SELECT) == (PreconditionPhase.SELECT,)
    assert phase_check_points(PreconditionPhase.MAINTAIN) == (PreconditionPhase.MAINTAIN,)
    assert phase_check_points(PreconditionPhase.ACCEPT) == (PreconditionPhase.ACCEPT,)


def test_condition_digest_is_stable_across_equal_conditions() -> None:
    world = World({"ready": TruthValue.TRUE})
    left = world.condition(world.atom("ready"))
    right = world.condition(world.atom("ready"))
    assert condition_digest(left) == condition_digest(right)


# --------------------------------------------------------------------------------------
# The plan pack's 13 schema fixtures, structural level
# --------------------------------------------------------------------------------------


def _codec_for(schema: str) -> Any:
    return {
        "method-contract-v1.schema.json": MethodContract.from_json,
        "plan-revision-proposal-v1.schema.json": PlanProposal.from_json,
        "goal-resolution-v1.schema.json": GoalResolution.from_json,
        "execution-feedback-v1.schema.json": ExecutionFeedbackV1.from_json,
    }[schema]


def test_all_thirteen_plan_fixtures_land_on_the_expected_side() -> None:
    fixtures = load_plan_fixtures()
    assert len(fixtures) == 13

    for fixture in fixtures:
        codec = _codec_for(fixture["schema"])
        name = fixture["name"]
        if fixture["expect_structure_valid"]:
            decoded = codec(fixture["payload"])
            assert decoded.to_json() == fixture["payload"], name
        else:
            with pytest.raises(ContractError):
                codec(fixture["payload"])


def test_the_structurally_valid_but_semantically_invalid_resolution_still_decodes() -> None:
    """Fixture 13: ACCEPT with a failing required criterion is a *domain* rejection.

    The codec's job ends at structure; refusing the ACCEPT is P1.1b's acceptance
    rules, and conflating the two would let a structural check masquerade as a
    semantic one.
    """

    fixture = next(
        item
        for item in load_plan_fixtures()
        if item["name"] == "resolution-structural-valid-semantic-invalid"
    )
    resolution = GoalResolution.from_json(fixture["payload"])
    assert resolution.verdict.value == "ACCEPT"
    assert any(item.verdict.value == "FAIL" for item in resolution.criteria)


def test_repo_copies_match_the_upstream_plan_pack() -> None:
    """The in-repo fixtures are a copy; this is what makes the copy honest.

    Wherever the plan pack itself is checked out, every copied file is compared
    against both the upstream bytes and the SHA-256 recorded in ``SOURCE.md``, so a
    specification change cannot sit unnoticed behind a stale duplicate.  Where the
    pack is absent (CI), only this comparison is skipped — the fixture tests above
    still run against the copies.
    """

    manifest = source_manifest()
    assert len(manifest) == 18

    for local, _upstream, digest in manifest:
        assert sha256_of(FIXTURE_ROOT / local) == digest, f"{local} drifted from SOURCE.md"

    if not upstream_plan_pack_available():
        pytest.skip("upstream FULL-TARGET-1.4 plan pack not present on this machine")

    for local, upstream, _digest in manifest:
        assert (FIXTURE_ROOT / local).read_bytes() == (UPSTREAM_ROOT / upstream).read_bytes(), (
            f"{local} no longer matches {upstream}"
        )


# --------------------------------------------------------------------------------------
# Schema boundaries: the caps are contract, not decoration
# --------------------------------------------------------------------------------------


def _schema_values(node: Any, keyword: str) -> set[Any]:
    """Collect every value the copied schemas give to one JSON-Schema keyword."""

    found: set[Any] = set()
    if isinstance(node, dict):
        for key, item in node.items():
            if key == keyword and not isinstance(item, (dict, list)):
                found.add(item)
            found |= _schema_values(item, keyword)
    elif isinstance(node, list):
        for item in node:
            found |= _schema_values(item, keyword)
    return found


def _all_plan_schemas() -> list[Any]:
    import json

    return [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((FIXTURE_ROOT / "plan_pack").glob("*.schema.json"))
    ]


def test_the_codec_caps_are_exactly_the_ones_the_schemas_declare() -> None:
    """Pin the limits themselves, so relaxing one is a failing test, not a silent win.

    A mutation that multiplies a cap keeps every round trip green — the payloads in
    the fixtures are nowhere near the limit — so the limit has to be asserted
    directly, against the schemas rather than against a second copy of the number.
    """

    schemas = _all_plan_schemas()
    assert _schema_values(schemas, "maxLength") == {512, 20_000}
    assert _schema_values(schemas, "minLength") == {1}
    assert _schema_values(schemas, "minimum") == {0, 1}

    assert MAX_ID == 512
    assert MAX_TEXT == 20_000


def test_the_list_cap_is_ours_and_is_stricter_than_the_schemas() -> None:
    """The plan schemas declare no ``maxItems``; MAX_LIST is a deployment bound.

    ADR-08 keeps hard resource ceilings even where the wire format states none, so
    this asymmetry is intended — and recorded here so nobody "fixes" it by removing
    the cap to match the schema.
    """

    assert _schema_values(_all_plan_schemas(), "maxItems") == set()
    assert MAX_LIST == 256


def test_an_id_of_exactly_the_maximum_length_is_accepted_and_one_more_is_not() -> None:
    at_limit = "x" * MAX_ID
    over_limit = "x" * (MAX_ID + 1)

    assert VersionedRef(id=at_limit, version=1, content_hash=HASH_A).id == at_limit
    with pytest.raises(ContractError, match=f"exceeds {MAX_ID} characters"):
        VersionedRef(id=over_limit, version=1, content_hash=HASH_A)


def test_a_text_field_of_exactly_the_maximum_length_is_accepted_and_one_more_is_not() -> None:
    from agent_orchestrator.contracts.htn import CriterionLink

    def link(statement: str) -> CriterionLink:
        return CriterionLink(
            parent_criterion_id="c-complete",
            child_step="extract",
            child_criterion_id="c-extracted",
            evidence_requirement=statement,
        )

    assert link("y" * MAX_TEXT).evidence_requirement == "y" * MAX_TEXT
    with pytest.raises(ContractError, match=f"exceeds {MAX_TEXT} characters"):
        link("y" * (MAX_TEXT + 1))


def test_a_list_of_exactly_the_maximum_length_is_accepted_and_one_more_is_not() -> None:
    def refs(count: int) -> tuple[str, ...]:
        return tuple(f"criterion-{index:04d}" for index in range(count))

    at_limit = GoalSignature(
        signature_id="compare-sources",
        version=1,
        parameter_schema_ref=vref("parameters"),
        output_schema_ref=vref("outputs"),
        statement="compare the named sources",
        coverage_criteria=refs(MAX_LIST),
    )
    assert len(at_limit.coverage_criteria) == MAX_LIST

    with pytest.raises(ContractError, match=f"more than {MAX_LIST} entries"):
        GoalSignature(
            signature_id="compare-sources",
            version=1,
            parameter_schema_ref=vref("parameters"),
            output_schema_ref=vref("outputs"),
            statement="compare the named sources",
            coverage_criteria=refs(MAX_LIST + 1),
        )


@pytest.mark.parametrize("blank", ["", " ", "\t\n"])
def test_minlength_one_rejects_a_blank_identifier_and_a_blank_text(blank: str) -> None:
    with pytest.raises(ContractError, match="must not be blank"):
        VersionedRef(id=blank, version=1, content_hash=HASH_A)


def test_a_revision_of_zero_is_accepted_and_a_negative_one_is_not() -> None:
    from agent_orchestrator.contracts.semantic_base import EvidenceRef, EvidenceRefKind

    at_floor = EvidenceRef(
        kind=EvidenceRefKind.OBSERVATION, id="observation-1", revision=0, content_hash=HASH_A
    )
    assert at_floor.revision == 0
    with pytest.raises(ContractError, match="must be >= 0"):
        EvidenceRef(
            kind=EvidenceRefKind.OBSERVATION, id="observation-1", revision=-1, content_hash=HASH_A
        )


def test_a_versioned_ref_version_starts_at_one() -> None:
    assert VersionedRef(id="schema-1", version=1, content_hash=HASH_A).version == 1
    with pytest.raises(ContractError, match="must be >= 1"):
        VersionedRef(id="schema-1", version=0, content_hash=HASH_A)


def test_a_minitems_one_array_refuses_to_be_empty() -> None:
    payload = plan_proposal().to_json()
    payload["read_set"] = []
    with pytest.raises(ContractError, match="at least 1 entries"):
        PlanProposal.from_json(payload)

    payload = plan_proposal().to_json()
    payload["operations"] = []
    with pytest.raises(ContractError, match="at least 1 entries"):
        PlanProposal.from_json(payload)


@pytest.mark.parametrize(
    "bad_hash",
    ["a" * 63, "a" * 65, "A" * 64, "g" * 64, "", "0x" + "a" * 62],
)
def test_a_content_hash_must_be_exactly_sixty_four_lowercase_hex_digits(bad_hash: str) -> None:
    with pytest.raises(ContractError, match="SHA-256 hex digest"):
        VersionedRef(id="schema-1", version=1, content_hash=bad_hash)


@pytest.mark.parametrize("bad_revision", ["1", 1.0, True, None])
def test_a_string_or_bool_never_passes_for_an_integer_revision(bad_revision: Any) -> None:
    payload = plan_proposal().to_json()
    payload["expected_plan_revision"] = bad_revision
    with pytest.raises(ContractError, match="must be an integer"):
        PlanProposal.from_json(payload)


# --------------------------------------------------------------------------------------
# Contract round 3: additions requested by P1.1b / P1.1c / P2.1b
# --------------------------------------------------------------------------------------


def _budget_payload() -> dict[str, int]:
    return {
        "budget_version": 1,
        "max_live_tasks": 2048,
        "max_depth": 64,
        "max_expanded_nodes": 8192,
        "max_candidates": 8,
        "max_recursion_fuel": 3,
        "max_nodes": 4096,
        "max_edges": 16_384,
        "max_fan_out": 256,
    }


def test_a_graph_structure_budget_is_versioned_and_positive() -> None:
    budget = GraphStructureBudget(**_budget_payload())

    assert GraphStructureBudget.from_json(budget.to_json()) == budget
    assert budget.max_recursion_fuel == 3
    # The planning bounds and the projection bounds are separate dimensions: a plan
    # can be within its depth and still project to a graph nobody should schedule.
    assert (budget.max_nodes, budget.max_edges, budget.max_fan_out) == (4096, 16_384, 256)


@pytest.mark.parametrize(
    "field_name",
    [
        "budget_version",
        "max_live_tasks",
        "max_depth",
        "max_expanded_nodes",
        "max_candidates",
        "max_recursion_fuel",
        "max_nodes",
        "max_edges",
        "max_fan_out",
    ],
)
def test_a_graph_budget_of_zero_is_not_a_budget(field_name: str) -> None:
    payload = _budget_payload()
    payload[field_name] = 0
    with pytest.raises(ContractError, match="must be >= 1"):
        GraphStructureBudget.from_json(payload)


def test_every_graph_budget_dimension_is_required() -> None:
    for field_name in _budget_payload():
        payload = _budget_payload()
        del payload[field_name]
        with pytest.raises(ContractError, match="missing required fields"):
            GraphStructureBudget.from_json(payload)


def test_a_set_port_may_declare_how_its_bindings_are_ordered() -> None:
    ordinal = PortSpec(
        port_key="sources",
        schema_ref=vref("source-list"),
        cardinality=PortCardinality.SET,
        ordering=PortOrdering.BY_PRODUCER_ORDINAL,
    )
    keyed = PortSpec(
        port_key="sources",
        schema_ref=vref("source-list"),
        cardinality=PortCardinality.SET,
        ordering=PortOrdering.BY_KEY,
        order_key="published_at",
    )

    assert PortSpec.from_json(ordinal.to_json()) == ordinal
    assert PortSpec.from_json(keyed.to_json()) == keyed
    assert ordinal.set_order_declared is True


def test_a_single_valued_port_has_no_ordering_to_declare() -> None:
    with pytest.raises(ContractError, match="no ordering"):
        PortSpec(
            port_key="report",
            schema_ref=vref("report"),
            cardinality=PortCardinality.SINGLE,
            ordering=PortOrdering.BY_PRODUCER_ORDINAL,
        )


def test_by_key_ordering_needs_the_key_and_the_others_refuse_one() -> None:
    with pytest.raises(ContractError, match="needs an order_key"):
        PortSpec(
            port_key="sources",
            schema_ref=vref("source-list"),
            cardinality=PortCardinality.SET,
            ordering=PortOrdering.BY_KEY,
        )
    with pytest.raises(ContractError, match="only meaningful for BY_KEY"):
        PortSpec(
            port_key="sources",
            schema_ref=vref("source-list"),
            cardinality=PortCardinality.SET,
            ordering=PortOrdering.EXPLICIT,
            order_key="published_at",
        )


def test_undeclared_set_ports_are_reported_for_the_compiler_to_refuse() -> None:
    """TG §4.3 wants every set port ordered; the contract reports, the compiler refuses.

    Keeping ``ordering`` optional lets a port list be assembled before the ordering
    decision is made — the check belongs where a whole network is admitted.
    """

    ports = (
        PortSpec(port_key="report", schema_ref=vref("report")),
        PortSpec(
            port_key="ordered",
            schema_ref=vref("list"),
            cardinality=PortCardinality.SET,
            ordering=PortOrdering.EXPLICIT,
        ),
        PortSpec(port_key="unordered", schema_ref=vref("list"), cardinality=PortCardinality.SET),
    )

    assert undeclared_set_ports(ports) == ("unordered",)


def test_an_unordered_port_keeps_the_bytes_it_had_before_the_field_existed() -> None:
    plain = PortSpec(port_key="report", schema_ref=vref("report"))
    assert "ordering" not in plain.to_json()
    assert "order_key" not in plain.to_json()


def test_a_primitive_binding_declares_what_it_reads_writes_and_does() -> None:
    binding = TaskSemanticBindingV1(
        task_id=TaskRef("task-1"),
        obligation_id=ObligationId("obligation-1"),
        contract_revision=ContractRevision(1),
        contract_hash=HASH_B,
        form=TaskForm.PRIMITIVE,
        goal_signature=goal_signature(),
        operator_ref=vref("operator-1"),
        resource_reads=(ResourceRef(namespace="workspace-1", object_id="src/report.md"),),
        resource_writes=(ResourceRef(namespace="workspace-1", object_id="out/report.md"),),
        side_effect_kind=SideEffectKind.LOCAL_WRITE,
    )

    assert TaskSemanticBindingV1.from_json(binding.to_json()) == binding
    assert binding.resource_writes[0].key == ("workspace-1", "out/report.md")


def test_a_compound_binding_declares_no_resources_or_side_effects() -> None:
    with pytest.raises(ContractError, match="compound task binding declares no resources"):
        TaskSemanticBindingV1(
            task_id=TaskRef("task-1"),
            obligation_id=ObligationId("obligation-1"),
            contract_revision=ContractRevision(1),
            contract_hash=HASH_B,
            form=TaskForm.COMPOUND,
            goal_signature=goal_signature(),
            side_effect_kind=SideEffectKind.EXTERNAL_STATE_WRITE,
        )


def test_a_binding_without_resources_keeps_its_earlier_content_hash() -> None:
    plain = task_binding()
    payload = plain.to_json()
    assert "resource_reads" not in payload
    assert "side_effect_kind" not in payload


def test_write_write_and_read_write_overlap_is_a_conflict_but_read_read_is_not() -> None:
    shared = ResourceRef(namespace="workspace-1", object_id="out/report.md")
    other = ResourceRef(namespace="workspace-1", object_id="src/notes.md")

    assert resource_conflicts((), (shared,), (), (shared,)) == (shared,)
    assert resource_conflicts((shared,), (), (), (shared,)) == (shared,)
    assert resource_conflicts((shared,), (), (shared,), ()) == ()
    assert resource_conflicts((), (other,), (), (shared,)) == ()


def test_the_same_path_in_two_namespaces_is_two_resources() -> None:
    left = ResourceRef(namespace="workspace-1", object_id="out/report.md")
    right = ResourceRef(namespace="workspace-2", object_id="out/report.md")
    assert resource_conflicts((), (left,), (), (right,)) == ()


@pytest.mark.parametrize(
    "relation,source,target",
    [
        (RelationKind.SUPPORT, EndpointKind.EVIDENCE, EndpointKind.TASK),
        (RelationKind.ASSUMPTION, EndpointKind.EVIDENCE, EndpointKind.METHOD_INSTANCE),
        (RelationKind.SUPERVISION, EndpointKind.SUPERVISOR, EndpointKind.OCCURRENCE),
        (RelationKind.FUNDING, EndpointKind.OBLIGATION, EndpointKind.OBLIGATION),
        (RelationKind.SUPERSEDES, EndpointKind.TASK, EndpointKind.TASK),
    ],
)
def test_each_typed_relation_accepts_its_own_endpoints(
    relation: RelationKind, source: EndpointKind, target: EndpointKind
) -> None:
    edge = TypedEdge(
        relation=relation,
        source=NetworkEndpoint(kind=source, id="left"),
        target=NetworkEndpoint(kind=target, id="right"),
        label="because",
    )
    assert TypedEdge.from_json(edge.to_json()) == edge


@pytest.mark.parametrize(
    "relation,source,target,message",
    [
        (RelationKind.SUPPORT, EndpointKind.TASK, EndpointKind.TASK, "source must be one of"),
        (
            RelationKind.SUPERVISION,
            EndpointKind.SUPERVISOR,
            EndpointKind.EVIDENCE,
            "target must be one of",
        ),
        (
            RelationKind.FUNDING,
            EndpointKind.TASK,
            EndpointKind.OBLIGATION,
            "source must be one of",
        ),
        (
            RelationKind.SUPERSEDES,
            EndpointKind.TASK,
            EndpointKind.OBLIGATION,
            "same kind",
        ),
    ],
)
def test_a_typed_relation_refuses_endpoints_that_do_not_belong_to_it(
    relation: RelationKind, source: EndpointKind, target: EndpointKind, message: str
) -> None:
    with pytest.raises(ContractError, match=message):
        TypedEdge(
            relation=relation,
            source=NetworkEndpoint(kind=source, id="left"),
            target=NetworkEndpoint(kind=target, id="right"),
        )


@pytest.mark.parametrize(
    "relation",
    [RelationKind.REFINEMENT, RelationKind.SATISFIES, RelationKind.ORDER, RelationKind.DATA],
)
def test_the_relations_with_their_own_contract_type_are_not_generic_edges(
    relation: RelationKind,
) -> None:
    """An execution dependency expressed as a generic edge would skip every check
    ``OrderConstraint`` and ``DataRequirement`` exist to make."""

    with pytest.raises(ContractError, match="own contract type"):
        TypedEdge(
            relation=relation,
            source=NetworkEndpoint(kind=EndpointKind.OCCURRENCE, id="left"),
            target=NetworkEndpoint(kind=EndpointKind.OCCURRENCE, id="right"),
        )


def test_the_task_binding_is_the_authority_for_obligation_and_form() -> None:
    binding = task_binding(task_id="task-2", obligation="obligation-9", form=TaskForm.PRIMITIVE)
    agreeing = OccurrenceSpec(
        occurrence_id=OccurrenceId("occurrence-1"),
        task_id=TaskRef("task-2"),
        obligation_id=ObligationId("obligation-9"),
        form=TaskForm.PRIMITIVE,
    )

    assert_occurrences_match_bindings((agreeing,), {binding.task_id: binding})


def test_an_occurrence_that_claims_another_duty_is_refused() -> None:
    binding = task_binding(task_id="task-2", obligation="obligation-9", form=TaskForm.PRIMITIVE)
    drifted = OccurrenceSpec(
        occurrence_id=OccurrenceId("occurrence-1"),
        task_id=TaskRef("task-2"),
        obligation_id=ObligationId("obligation-other"),
        form=TaskForm.PRIMITIVE,
    )

    with pytest.raises(ContractError, match="the binding is the authority"):
        assert_occurrences_match_bindings((drifted,), {binding.task_id: binding})


def test_an_occurrence_that_claims_another_form_is_refused() -> None:
    binding = task_binding(task_id="task-2", obligation="obligation-9", form=TaskForm.PRIMITIVE)
    drifted = OccurrenceSpec(
        occurrence_id=OccurrenceId("occurrence-1"),
        task_id=TaskRef("task-2"),
        obligation_id=ObligationId("obligation-9"),
        form=TaskForm.COMPOUND,
    )

    with pytest.raises(ContractError, match="the binding is the authority"):
        assert_occurrences_match_bindings((drifted,), {binding.task_id: binding})


def test_an_occurrence_without_a_binding_is_refused() -> None:
    with pytest.raises(ContractError, match="no semantic binding"):
        assert_occurrences_match_bindings(
            (
                OccurrenceSpec(
                    occurrence_id=OccurrenceId("occurrence-1"),
                    task_id=TaskRef("task-unknown"),
                    obligation_id=ObligationId("obligation-1"),
                    form=TaskForm.PRIMITIVE,
                ),
            ),
            {},
        )


def test_a_delta_checks_its_own_occurrences_against_the_bindings() -> None:
    delta = proposed_delta()
    bindings = {
        TaskRef("task-2"): task_binding(
            task_id="task-2", obligation="obligation-2", form=TaskForm.PRIMITIVE
        ),
        TaskRef("task-3"): task_binding(
            task_id="task-3", obligation="obligation-3", form=TaskForm.PRIMITIVE
        ),
    }

    delta.assert_consistent_with(bindings)

    bindings[TaskRef("task-3")] = task_binding(
        task_id="task-3", obligation="obligation-3", form=TaskForm.COMPOUND
    )
    with pytest.raises(ContractError, match="the binding is the authority"):
        delta.assert_consistent_with(bindings)


def test_a_proposal_may_attribute_work_to_a_model_but_not_to_a_tool() -> None:
    payload = plan_proposal().to_json()
    payload["trigger_refs"] = [
        {
            "kind": "observation",
            "id": "observation-1",
            "revision": 1,
            "content_hash": HASH_A,
            "produced_by": "model",
        }
    ]
    assert PlanProposal.from_json(payload).trigger_refs[0].produced_by is Provenance.MODEL

    payload["trigger_refs"][0]["produced_by"] = "tool"
    with pytest.raises(ContractError, match="may not claim 'tool'"):
        PlanProposal.from_json(payload)


def test_worker_feedback_may_not_attribute_its_own_evidence_to_the_system() -> None:
    fixture = next(
        item for item in load_plan_fixtures() if item["name"] == "execution-feedback-v1-valid"
    )
    payload = dict(fixture["payload"])
    assert ExecutionFeedbackV1.from_json(payload).outcome.value == "blocked"

    import copy

    tampered = copy.deepcopy(payload)
    tampered["observations"][0]["evidence_refs"][0]["produced_by"] = "system"
    with pytest.raises(ContractError, match="may not claim 'system'"):
        ExecutionFeedbackV1.from_json(tampered)


def test_an_unattributed_reference_keeps_the_bytes_it_had_before_provenance_existed() -> None:
    from agent_orchestrator.contracts.semantic_base import EvidenceRef, EvidenceRefKind

    plain = EvidenceRef(
        kind=EvidenceRefKind.OBSERVATION, id="observation-1", revision=1, content_hash=HASH_A
    )
    assert "produced_by" not in plain.to_json()
    assert EvidenceRef.from_json(plain.to_json()) == plain


# --------------------------------------------------------------------------------------
# Contract round 3 (CR-6): a shared compound goal may sit at several occurrences
# --------------------------------------------------------------------------------------


def _compound_occurrence(occurrence: str, task: str, obligation: str) -> OccurrenceSpec:
    return OccurrenceSpec(
        occurrence_id=OccurrenceId(occurrence),
        task_id=TaskRef(task),
        obligation_id=ObligationId(obligation),
        form=TaskForm.COMPOUND,
    )


def _draft_at(instance: str, goal: str, occurrence: str | None) -> MethodInstanceDraft:
    return MethodInstanceDraft(
        instance_id=instance,  # type: ignore[arg-type]
        goal_id=TaskRef(goal),
        obligation_id=ObligationId("obligation-1"),
        method_ref=method_contract().method_ref(),
        goal_occurrence_id=None if occurrence is None else OccurrenceId(occurrence),
    )


def test_a_draft_without_a_goal_occurrence_derives_one_from_its_goal() -> None:
    draft = _draft_at("instance-1", "task-1", None)
    assert draft.goal_occurrence_id is None
    assert str(draft.effective_goal_occurrence_id) == "task-1"
    assert "goal_occurrence_id" not in draft.to_json()
    assert MethodInstanceDraft.from_json(draft.to_json()) == draft


def test_two_consumers_may_refine_two_occurrences_of_one_shared_goal() -> None:
    """TG §12: sharing a sub-goal reuses the result without either consumer owning it."""

    occurrences = (
        _compound_occurrence("occurrence-a", "task-shared", "obligation-1"),
        _compound_occurrence("occurrence-b", "task-shared", "obligation-1"),
    )
    drafts = (
        _draft_at("instance-a", "task-shared", "occurrence-a"),
        _draft_at("instance-b", "task-shared", "occurrence-b"),
    )

    assert_method_instances_match_occurrences(drafts, occurrences)
    assert drafts[0].effective_goal_occurrence_id != drafts[1].effective_goal_occurrence_id
    assert MethodInstanceDraft.from_json(drafts[0].to_json()) == drafts[0]


def test_a_draft_that_refines_another_task_s_occurrence_is_refused() -> None:
    occurrences = (_compound_occurrence("occurrence-a", "task-other", "obligation-1"),)
    drafts = (_draft_at("instance-a", "task-shared", "occurrence-a"),)

    with pytest.raises(ContractError, match="belongs to task"):
        assert_method_instances_match_occurrences(drafts, occurrences)


def test_a_draft_may_not_refine_a_primitive_occurrence() -> None:
    occurrences = (
        OccurrenceSpec(
            occurrence_id=OccurrenceId("occurrence-a"),
            task_id=TaskRef("task-shared"),
            obligation_id=ObligationId("obligation-1"),
            form=TaskForm.PRIMITIVE,
        ),
    )
    drafts = (_draft_at("instance-a", "task-shared", "occurrence-a"),)

    with pytest.raises(ContractError, match="which is primitive"):
        assert_method_instances_match_occurrences(drafts, occurrences)


def test_a_draft_naming_an_occurrence_this_delta_does_not_carry_is_left_to_the_network() -> None:
    drafts = (_draft_at("instance-a", "task-shared", "occurrence-elsewhere"),)
    assert_method_instances_match_occurrences(drafts, ())


def test_a_slot_may_bind_a_shared_goal_occurrence_when_it_is_reusing_it() -> None:
    shared = ChildBinding(
        instance_id="instance-1",  # type: ignore[arg-type]
        slot_key="extract",
        occurrence_id=OccurrenceId("occurrence-mine"),
        obligation_id=ObligationId("obligation-2"),
        reuse_policy=ReusePolicy.SHARE_ACTIVE,
        goal_occurrence_id=OccurrenceId("occurrence-shared"),
    )

    assert ChildBinding.from_json(shared.to_json()) == shared
    assert str(shared.goal_occurrence_id) == "occurrence-shared"


def test_a_new_work_slot_may_not_point_at_someone_else_s_occurrence() -> None:
    """NEW_WORK means this slot creates the work; pointing elsewhere is reuse or sharing."""

    with pytest.raises(ContractError, match="NEW_WORK means this slot creates the work"):
        ChildBinding(
            instance_id="instance-1",  # type: ignore[arg-type]
            slot_key="extract",
            occurrence_id=OccurrenceId("occurrence-mine"),
            obligation_id=ObligationId("obligation-2"),
            reuse_policy=ReusePolicy.NEW_WORK,
            goal_occurrence_id=OccurrenceId("occurrence-shared"),
        )


def test_an_unshared_child_binding_keeps_the_bytes_it_had_before_the_field_existed() -> None:
    plain = ChildBinding(
        instance_id="instance-1",  # type: ignore[arg-type]
        slot_key="extract",
        occurrence_id=OccurrenceId("occurrence-1"),
        obligation_id=ObligationId("obligation-2"),
    )
    assert "goal_occurrence_id" not in plain.to_json()


# --------------------------------------------------------------------------------------
# Contract round 4: MethodRegistration travels through its codec (P1.2)
# --------------------------------------------------------------------------------------


def test_a_method_registration_round_trips_through_its_codec() -> None:
    registration = admit_method(
        method_contract().method_ref(),
        MethodRegistryStatus.TRIAL_ADMITTED,
        author=RegistryAuthor.SYSTEM,
        admission_receipt_ref=tref(TypedRefKind.REVIEW, "admission-1"),
        trial_scope_mission="mission-1",
    )

    restored = MethodRegistration.from_json(registration.to_json())

    assert restored == registration
    assert restored.to_json() == registration.to_json()


def test_a_stored_registration_cannot_restore_a_promotion_that_was_never_granted() -> None:
    """Rebuilding through the codec re-runs the admission rules on the way back in."""

    payload = admit_method(
        method_contract().method_ref(),
        MethodRegistryStatus.DRAFT,
        author=RegistryAuthor.MODEL,
    ).to_json()
    payload["status"] = "ADMITTED"

    with pytest.raises(ContractError, match="registry_status past DRAFT"):
        MethodRegistration.from_json(payload)


def test_a_stored_trial_admission_still_needs_its_mission_scope() -> None:
    payload = admit_method(
        method_contract().method_ref(),
        MethodRegistryStatus.TRIAL_ADMITTED,
        author=RegistryAuthor.SYSTEM,
        trial_scope_mission="mission-1",
    ).to_json()
    payload["trial_scope_mission"] = None

    with pytest.raises(ContractError, match="scoped to one mission"):
        MethodRegistration.from_json(payload)


def test_a_bound_input_already_travels_through_a_codec() -> None:
    bound = BoundInput(
        requirement_id="requirement-1",
        producer_result_id="result-1",
        acceptance_id="acceptance-1",
        artifact_id="artifact-1",
        content_hash=HASH_A,
        schema_ref=vref("evidence"),
        source_revision="rev-3",
    )
    assert BoundInput.from_json(bound.to_json()) == bound


# --------------------------------------------------------------------------------------
# Contract round 5: control read channels, human authorship, reuse declarations
# --------------------------------------------------------------------------------------


def test_the_read_set_carries_obligation_and_authority_channels() -> None:
    """A stale duty and a revoked authority fail a commit for different reasons."""

    enriched = SemanticReadSet(
        requirements_revision=3,
        obligation_revisions=(
            ReadItem(
                kind=ReadItemKind.OBLIGATION,
                id="obligation-1",
                semantic_revision=4,
                content_hash=HASH_A,
            ),
        ),
        authority_revisions=(
            ReadItem(
                kind=ReadItemKind.AUTHORITY,
                id="grant-1",
                semantic_revision=2,
                content_hash=HASH_A,
            ),
        ),
    )

    restored = SemanticReadSet.from_json(enriched.to_json())
    assert restored == enriched
    assert restored.obligation_revisions[0].id == "obligation-1"
    assert restored.authority_revisions[0].id == "grant-1"


@pytest.mark.parametrize(
    "channel,wrong_kind",
    [
        ("obligation_revisions", ReadItemKind.AUTHORITY),
        ("authority_revisions", ReadItemKind.OBLIGATION),
    ],
)
def test_a_read_channel_refuses_an_entry_of_another_kind(
    channel: str, wrong_kind: ReadItemKind
) -> None:
    item = ReadItem(kind=wrong_kind, id="x-1", semantic_revision=1, content_hash=HASH_A)
    with pytest.raises(ContractError, match="entries must be of kind"):
        SemanticReadSet(requirements_revision=1, **{channel: (item,)})


def test_a_read_set_without_the_new_channels_keeps_its_earlier_bytes() -> None:
    payload = read_set().to_json()
    assert "obligation_revisions" not in payload
    assert "authority_revisions" not in payload
    assert SemanticReadSet.from_json(payload) == read_set()


def test_a_hand_written_seed_method_is_recorded_as_human_authored() -> None:
    """Recording a person's work as SYSTEM loses the one fact the field exists for."""

    registration = admit_method(
        method_contract().method_ref(),
        MethodRegistryStatus.ADMITTED,
        author=RegistryAuthor.HUMAN,
    )

    assert registration.author is RegistryAuthor.HUMAN
    assert MethodRegistration.from_json(registration.to_json()) == registration


def test_human_authorship_does_not_relax_the_model_promotion_rule() -> None:
    payload = admit_method(
        method_contract().method_ref(),
        MethodRegistryStatus.DRAFT,
        author=RegistryAuthor.MODEL,
    ).to_json()
    payload["author"] = "human"
    assert MethodRegistration.from_json(payload).author is RegistryAuthor.HUMAN

    payload["author"] = "model"
    payload["status"] = "ADMITTED"
    with pytest.raises(ContractError, match="registry_status past DRAFT"):
        MethodRegistration.from_json(payload)


def test_a_method_step_may_declare_how_its_work_is_de_duplicated() -> None:
    step = MethodStep(
        local_id="extract",
        task_type_ref=vref("extract-evidence"),
        form=TaskForm.PRIMITIVE,
        arguments={},
        required_capabilities=("sources.read",),
        obligation_relation=ObligationRelation.REFINES_PARENT,
        reuse_policy=ReusePolicy.REUSE_ACCEPTED,
    )

    assert step.to_json()["reuse_policy"] == "reuse_accepted"
    assert (
        MethodStep.from_json(step.to_json(), "step", StructureBudget(MAX_CONDITION_NODES)) == step
    )


def test_a_step_that_declares_no_reuse_policy_keeps_the_published_schema_bytes() -> None:
    """``method-contract-v1`` does not declare this field, so an unset step is unchanged."""

    contract = method_contract()
    assert "reuse_policy" not in contract.steps[0].to_json()
    assert contract.steps[0].reuse_policy is None


def test_a_method_step_refuses_an_unknown_reuse_policy() -> None:
    payload = MethodStep(
        local_id="extract",
        task_type_ref=vref("extract-evidence"),
        form=TaskForm.PRIMITIVE,
        arguments={},
        required_capabilities=(),
        obligation_relation=ObligationRelation.REFINES_PARENT,
    ).to_json()
    payload["reuse_policy"] = "whatever_is_cheapest"

    with pytest.raises(ContractError, match="must be one of"):
        MethodStep.from_json(payload, "step", StructureBudget(MAX_CONDITION_NODES))


def test_a_reusing_slot_may_name_the_exact_acceptance_it_reuses() -> None:
    binding = ChildBinding(
        instance_id="instance-1",  # type: ignore[arg-type]
        slot_key="extract",
        occurrence_id=OccurrenceId("occurrence-1"),
        obligation_id=ObligationId("obligation-2"),
        reuse_policy=ReusePolicy.REUSE_ACCEPTED,
        acceptance_ref=tref(TypedRefKind.ACCEPTANCE, "acceptance-1"),
    )

    assert ChildBinding.from_json(binding.to_json()) == binding
    assert binding.acceptance_ref is not None
    assert binding.acceptance_ref.id == "acceptance-1"


@pytest.mark.parametrize("policy", [ReusePolicy.NEW_WORK, ReusePolicy.SHARE_ACTIVE])
def test_a_slot_that_is_not_reusing_has_no_acceptance_to_point_at(policy: ReusePolicy) -> None:
    """Sharing live work has no acceptance yet, and new work has nothing to point at."""

    with pytest.raises(ContractError, match="no accepted result to point at"):
        ChildBinding(
            instance_id="instance-1",  # type: ignore[arg-type]
            slot_key="extract",
            occurrence_id=OccurrenceId("occurrence-1"),
            obligation_id=ObligationId("obligation-2"),
            reuse_policy=policy,
            acceptance_ref=tref(TypedRefKind.ACCEPTANCE, "acceptance-1"),
        )


def test_a_slot_without_an_acceptance_ref_keeps_the_bytes_it_had_before() -> None:
    plain = ChildBinding(
        instance_id="instance-1",  # type: ignore[arg-type]
        slot_key="extract",
        occurrence_id=OccurrenceId("occurrence-1"),
        obligation_id=ObligationId("obligation-2"),
    )
    assert "acceptance_ref" not in plain.to_json()


# --------------------------------------------------------------------------------------
# Contract round 6 (CR#6): a delta says which duties it opens
# --------------------------------------------------------------------------------------


def _opening(obligation: str = "obligation-new", parent: str = "obligation-1") -> ObligationOpening:
    return ObligationOpening(
        obligation_id=ObligationId(obligation),
        parent_obligation_id=ObligationId(parent),
        relation=ObligationRelation.REFINES_PARENT,
        requirement_refs=("c-complete",),
        goal_signature=goal_signature(),
        budget_inheritance=BudgetInheritance.INHERIT_PARENT_FUEL_SHARE,
        fuel_share=1,
    )


def test_a_delta_carries_the_duties_it_opens_and_round_trips() -> None:
    delta = proposed_delta()
    with_openings = ProposedPlanDelta(
        delta_id=delta.delta_id,
        mission_id=delta.mission_id,
        base_plan_revision=delta.base_plan_revision,
        read_set=delta.read_set,
        occurrences=delta.occurrences,
        obligation_openings=(_opening("obligation-2"), _opening("obligation-3")),
    )

    restored = ProposedPlanDelta.from_json(with_openings.to_json())
    assert restored == with_openings
    assert len(restored.obligation_openings) == 2


def test_a_delta_that_opens_nothing_keeps_its_earlier_bytes() -> None:
    assert "obligation_openings" not in proposed_delta().to_json()


def test_a_delta_may_not_open_one_duty_twice() -> None:
    with pytest.raises(ContractError, match="must not open one duty twice"):
        ProposedPlanDelta(
            delta_id="delta-1",
            mission_id="mission-1",  # type: ignore[arg-type]
            base_plan_revision=PlanRevision(1),
            read_set=read_set(),
            obligation_openings=(_opening("obligation-2"), _opening("obligation-2")),
        )


def test_an_occurrence_for_a_duty_nobody_opened_is_refused_at_commit() -> None:
    """Otherwise the ledger invents the duty — with a fresh failure count (§6.1)."""

    delta = proposed_delta()

    with pytest.raises(ContractError, match="names unregistered duty"):
        require_commit_ready(
            delta, registered_obligations=frozenset({ObligationId("obligation-2")})
        )


def test_an_occurrence_whose_duty_this_delta_opens_is_accepted() -> None:
    delta = proposed_delta()
    with_openings = ProposedPlanDelta(
        delta_id=delta.delta_id,
        mission_id=delta.mission_id,
        base_plan_revision=delta.base_plan_revision,
        read_set=delta.read_set,
        occurrences=delta.occurrences,
        obligation_openings=(_opening("obligation-3"),),
    )

    assert (
        require_commit_ready(
            with_openings, registered_obligations=frozenset({ObligationId("obligation-2")})
        )
        is with_openings
    )


def test_a_delta_may_not_re_open_a_duty_that_already_exists() -> None:
    delta = ProposedPlanDelta(
        delta_id="delta-1",
        mission_id="mission-1",  # type: ignore[arg-type]
        base_plan_revision=PlanRevision(1),
        read_set=read_set(),
        obligation_openings=(_opening("obligation-2"),),
    )

    with pytest.raises(ContractError, match="already exist"):
        require_commit_ready(
            delta, registered_obligations=frozenset({ObligationId("obligation-2")})
        )


def test_commit_readiness_without_a_registered_set_still_only_checks_the_shape() -> None:
    """The duty check is opt-in: a caller that has no ledger to consult is not lying."""

    delta = proposed_delta()
    assert require_commit_ready(delta) is delta
