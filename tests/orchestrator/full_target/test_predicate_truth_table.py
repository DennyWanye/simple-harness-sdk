# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""P1.1 red tests: the four-valued truth tables and the authorisation gate (§6.6).

These pin the rules the plan says an implementer must not "helpfully correct":
``NOT`` preserves UNKNOWN and CONFLICT, the ALL / ANY priority tables are not
``(t, f)`` arithmetic, an empty ALL is TRUE but never authorising, and a closed
domain denies only on an authoritative negative observation.
"""

from __future__ import annotations

import itertools
from typing import Any

import pytest
from full_target_world import (
    World,
    all_of,
    any_of,
    method_contract,
    not_of,
    task_binding,
    tref,
)

from agent_orchestrator.contracts.evidence_state import (
    EvidenceEntry,
    EvidenceSnapshot,
    ObservationRecord,
    PreconditionPhase,
    PreconditionWitnessRecord,
    QueryCompleteness,
    SupportCount,
    TruthValue,
    Validity,
)
from agent_orchestrator.contracts.htn import (
    MAX_CONDITION_NODES,
    MethodInstanceDraft,
    ObligationId,
    TaskRef,
    condition_digest,
    parse_condition,
)
from agent_orchestrator.contracts.models import ContractError
from agent_orchestrator.contracts.semantic_base import TypedRefKind, VersionedRef
from agent_orchestrator.knowledge.predicates import (
    ArgumentType,
    PredicateParameter,
    PredicateRegistry,
    PredicateSignature,
    WorldAssumption,
    atom_truth,
    authoritative_negative_matches_observer,
    closed_world_denial_admissible,
    proposition_key,
)
from agent_orchestrator.planning.htn.applicability import (
    ApplicabilityStatus,
    CapabilityRecord,
    CapabilitySnapshot,
    RecheckStatus,
    SupportProvenance,
    all_truth,
    any_truth,
    assess_method,
    authorization_gate,
    evaluate_condition,
    not_truth,
    recheck_method_instance,
)

VALUES = (TruthValue.TRUE, TruthValue.FALSE, TruthValue.UNKNOWN, TruthValue.CONFLICT)

NOT_TABLE = {
    TruthValue.TRUE: TruthValue.FALSE,
    TruthValue.FALSE: TruthValue.TRUE,
    TruthValue.UNKNOWN: TruthValue.UNKNOWN,
    TruthValue.CONFLICT: TruthValue.CONFLICT,
}


def _expected_all(values: tuple[TruthValue, ...]) -> TruthValue:
    for candidate in (TruthValue.FALSE, TruthValue.CONFLICT, TruthValue.UNKNOWN):
        if candidate in values:
            return candidate
    return TruthValue.TRUE


def _expected_any(values: tuple[TruthValue, ...]) -> TruthValue:
    for candidate in (TruthValue.TRUE, TruthValue.CONFLICT, TruthValue.UNKNOWN):
        if candidate in values:
            return candidate
    return TruthValue.FALSE


# --------------------------------------------------------------------------------------
# The tables themselves
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("value", VALUES)
def test_not_preserves_unknown_and_conflict(value: TruthValue) -> None:
    assert not_truth(value) is NOT_TABLE[value]


@pytest.mark.parametrize("left,right", list(itertools.product(VALUES, VALUES)))
def test_all_and_any_full_tables(left: TruthValue, right: TruthValue) -> None:
    assert all_truth((left, right)) is _expected_all((left, right))
    assert any_truth((left, right)) is _expected_any((left, right))


def test_empty_all_is_true_and_empty_any_is_false() -> None:
    assert all_truth(()) is TruthValue.TRUE
    assert any_truth(()) is TruthValue.FALSE


# --------------------------------------------------------------------------------------
# The two counterexamples §6.6 v1.4 pins against (t, f) arithmetic
# --------------------------------------------------------------------------------------


def test_any_unknown_conflict_is_conflict_not_true() -> None:
    """``(t, f)``'s ``or`` would make this (1, 0) = TRUE and let the work through."""

    world = World({"known": TruthValue.CONFLICT}, declared_only=frozenset({"absent"}))
    condition = parse_condition(any_of(world.atom("absent"), world.atom("known")))
    result = evaluate_condition(
        condition, registry=world.registry, snapshot=world.snapshot, parameters={}
    )
    assert result.truth is TruthValue.CONFLICT
    assert result.truth is not TruthValue.TRUE
    assert authorization_gate(result).allowed is False


def test_all_unknown_conflict_is_conflict_not_false() -> None:
    """``(t, f)``'s ``and`` would make this FALSE and report a decided negative."""

    world = World({"known": TruthValue.CONFLICT}, declared_only=frozenset({"absent"}))
    condition = parse_condition(all_of(world.atom("absent"), world.atom("known")))
    result = evaluate_condition(
        condition, registry=world.registry, snapshot=world.snapshot, parameters={}
    )
    assert result.truth is TruthValue.CONFLICT
    assert result.truth is not TruthValue.FALSE


@pytest.mark.parametrize("value", [TruthValue.UNKNOWN, TruthValue.CONFLICT])
def test_all_p_and_not_p_is_not_false_for_unknown_or_conflict(value: TruthValue) -> None:
    world = World({"p": value})
    condition = parse_condition(all_of(world.atom("p"), not_of(world.atom("p"))))
    result = evaluate_condition(
        condition, registry=world.registry, snapshot=world.snapshot, parameters={}
    )
    assert result.truth is value
    assert result.truth is not TruthValue.FALSE


def test_all_p_and_not_p_is_false_when_p_is_known() -> None:
    world = World({"p": TruthValue.TRUE})
    condition = parse_condition(all_of(world.atom("p"), not_of(world.atom("p"))))
    result = evaluate_condition(
        condition, registry=world.registry, snapshot=world.snapshot, parameters={}
    )
    assert result.truth is TruthValue.FALSE


# --------------------------------------------------------------------------------------
# Open / closed world
# --------------------------------------------------------------------------------------


def test_missing_record_is_unknown_in_an_open_domain() -> None:
    world = World({}, declared_only=frozenset({"p"}))
    condition = parse_condition(world.atom("p"))
    result = evaluate_condition(
        condition, registry=world.registry, snapshot=world.snapshot, parameters={}
    )
    assert result.truth is TruthValue.UNKNOWN
    assert result.provenance == frozenset({SupportProvenance.MISSING})


def test_closed_domain_without_an_authoritative_denial_is_unknown() -> None:
    """§6.6 C28: no negation as failure, not even in a closed domain."""

    world = World({}, closed=frozenset({"p"}))
    condition = parse_condition(world.atom("p"))
    result = evaluate_condition(
        condition, registry=world.registry, snapshot=world.snapshot, parameters={}
    )
    assert result.truth is TruthValue.UNKNOWN
    signature = world.signatures["p"]
    assert signature.world_assumption is WorldAssumption.CLOSED
    assert closed_world_denial_admissible(signature, None) is False
    assert atom_truth(signature, None) is TruthValue.UNKNOWN


def test_closed_domain_denies_only_with_a_watermarked_negative_observation() -> None:
    world = World({"p": TruthValue.FALSE}, closed=frozenset({"p"}))
    entry = world.snapshot.lookup(world.keys["p"])
    assert entry is not None
    assert closed_world_denial_admissible(world.signatures["p"], entry) is True
    condition = parse_condition(world.atom("p"))
    result = evaluate_condition(
        condition, registry=world.registry, snapshot=world.snapshot, parameters={}
    )
    assert result.truth is TruthValue.FALSE
    assert result.provenance == frozenset({SupportProvenance.CLOSED_WORLD_AUTHORITATIVE})


def test_a_counter_observation_is_not_diluted_by_positive_support() -> None:
    assert SupportCount(3, 1).truth is TruthValue.CONFLICT
    assert SupportCount(9, 0).truth is TruthValue.TRUE
    assert SupportCount(0, 0).truth is TruthValue.UNKNOWN
    assert SupportCount(1, 0).merge(SupportCount(0, 1)).truth is TruthValue.CONFLICT


def test_negative_support_requires_a_recorded_counter_observation() -> None:
    with pytest.raises(ContractError, match="missing record never produces"):
        EvidenceEntry(proposition_key="p", support=SupportCount(0, 1))


def test_stale_support_degrades_to_unknown_rather_than_flipping() -> None:
    world = World({"p": TruthValue.TRUE})
    stale = EvidenceEntry(
        proposition_key=world.keys["p"],
        support=SupportCount(1, 0),
        validity=Validity.STALE,
    )
    snapshot = EvidenceSnapshot(
        snapshot_id="snapshot-stale",
        as_of_ms=2_000,
        scope_id="mission-1",
        scope_epoch=2,
        support_revision=2,
        entries=(stale,),
    )
    result = evaluate_condition(
        parse_condition(world.atom("p")),
        registry=world.registry,
        snapshot=snapshot,
        parameters={},
    )
    assert result.truth is TruthValue.UNKNOWN
    assert result.provenance == frozenset({SupportProvenance.STALE})
    assert authorization_gate(result).allowed is False


# --------------------------------------------------------------------------------------
# Authorisation gate (§6.6 rule 2, invariant I18)
# --------------------------------------------------------------------------------------


def test_a_fully_observed_true_authorises() -> None:
    world = World({"p": TruthValue.TRUE, "q": TruthValue.TRUE})
    result = evaluate_condition(
        parse_condition(all_of(world.atom("p"), world.atom("q"))),
        registry=world.registry,
        snapshot=world.snapshot,
        parameters={},
    )
    decision = authorization_gate(result)
    assert result.truth is TruthValue.TRUE
    assert decision.allowed is True


def test_a_mixed_world_true_does_not_authorise() -> None:
    """A TRUE that partly rests on "nothing recorded" is not a fact about the world."""

    world = World({"p": TruthValue.TRUE}, closed=frozenset({"absent"}))
    result = evaluate_condition(
        parse_condition(any_of(world.atom("p"), world.atom("absent"))),
        registry=world.registry,
        snapshot=world.snapshot,
        parameters={},
    )
    assert result.truth is TruthValue.TRUE
    decision = authorization_gate(result)
    assert decision.allowed is False
    assert "missing" in decision.reason


def test_an_empty_expression_never_authorises() -> None:
    world = World({})
    result = evaluate_condition(
        parse_condition(all_of()),
        registry=world.registry,
        snapshot=world.snapshot,
        parameters={},
    )
    assert result.truth is TruthValue.TRUE
    assert authorization_gate(result).allowed is False


@pytest.mark.parametrize("value", [TruthValue.UNKNOWN, TruthValue.CONFLICT, TruthValue.FALSE])
def test_non_true_never_authorises(value: TruthValue) -> None:
    world = World({"p": value})
    result = evaluate_condition(
        parse_condition(world.atom("p")),
        registry=world.registry,
        snapshot=world.snapshot,
        parameters={},
    )
    assert authorization_gate(result).allowed is False


# --------------------------------------------------------------------------------------
# The interpreter accepts structure only (§7.3 step 1)
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        "truth_value == 'TRUE'",
        {"op": "predicate", "predicate_ref": "eval('True')", "arguments": {}},
        {"op": "python", "source": "return True"},
        {"op": "all", "items": [{"op": "sql", "query": "SELECT 1 FROM tasks"}]},
    ],
)
def test_the_ast_refuses_code_and_unknown_operators(payload: object) -> None:
    with pytest.raises(ContractError):
        parse_condition(payload)


def test_the_ast_refuses_a_callable() -> None:
    with pytest.raises(ContractError, match="callable"):
        parse_condition({"op": "constant", "value": len})


def test_the_ast_refuses_an_sql_fragment_in_an_argument() -> None:
    with pytest.raises(ContractError, match="executable code or a query fragment"):
        parse_condition(
            {
                "op": "predicate",
                "predicate_ref": {"id": "p", "version": 1, "content_hash": "a" * 64},
                "arguments": {"q": {"op": "constant", "value": "SELECT * FROM secrets"}},
            }
        )


def test_the_ast_refuses_more_nodes_than_the_structure_budget() -> None:
    payload: dict[str, object] = {"op": "constant", "value": True}
    for _ in range(MAX_CONDITION_NODES + 2):
        payload = {"op": "not", "item": payload}
    with pytest.raises(ContractError, match="structure budget"):
        parse_condition(payload)


def test_an_unregistered_predicate_is_a_type_error_not_a_verdict() -> None:
    world = World({"p": TruthValue.TRUE})
    result = evaluate_condition(
        parse_condition(world.atom("p")),
        registry=PredicateRegistry(),
        snapshot=world.snapshot,
        parameters={},
    )
    assert result.truth is TruthValue.UNKNOWN
    assert result.type_errors
    assert authorization_gate(result).allowed is False


def test_proposition_identity_is_predicate_version_plus_arguments() -> None:
    world = World({"p": TruthValue.TRUE})
    signature = world.signatures["p"]
    assert proposition_key(signature, {"subject": "alpha"}) != proposition_key(
        signature, {"subject": "beta"}
    )
    assert proposition_key(signature, {"subject": "alpha"}) == proposition_key(
        signature, {"subject": "alpha"}
    )


# --------------------------------------------------------------------------------------
# assess_method (§18.3): applicable / false / needs evidence / conflict / unavailable
# --------------------------------------------------------------------------------------


def _capabilities(*names: str) -> CapabilitySnapshot:
    return CapabilitySnapshot(records=tuple(CapabilityRecord(capability_id=item) for item in names))


def _assess(
    truth: TruthValue,
    *,
    capabilities: CapabilitySnapshot | None = None,
    atoms: tuple[str, ...] = ("ready",),
) -> tuple[World, Any]:
    world = World({name: truth for name in atoms})
    contract = method_contract(applicable_when=tuple(world.atom(name) for name in atoms))
    report = assess_method(
        task_binding(),
        contract,
        world.snapshot,
        capabilities if capabilities is not None else _capabilities("sources.read"),
        registry=world.registry,
    )
    return world, report


def test_assess_method_reports_applicable_when_every_precondition_is_observed_true() -> None:
    _, report = _assess(TruthValue.TRUE)
    assert report.status is ApplicabilityStatus.APPLICABLE
    assert report.applicable is True
    assert report.authorization is not None and report.authorization.allowed is True


def test_assess_method_separates_false_unknown_and_conflict() -> None:
    assert _assess(TruthValue.FALSE)[1].status is ApplicabilityStatus.PRECONDITION_FALSE
    unknown = _assess(TruthValue.UNKNOWN)[1]
    assert unknown.status is ApplicabilityStatus.NEEDS_EVIDENCE
    assert unknown.needs_evidence
    conflict = _assess(TruthValue.CONFLICT)[1]
    assert conflict.status is ApplicabilityStatus.CONFLICT
    assert conflict.conflicts


def test_assess_method_reports_a_capability_the_deployment_does_not_have() -> None:
    _, report = _assess(TruthValue.TRUE, capabilities=_capabilities())
    assert report.status is ApplicabilityStatus.CAPABILITY_UNAVAILABLE
    assert report.unmet_capabilities == ("sources.read",)


def test_assess_method_reports_an_unhealthy_capability_as_unavailable() -> None:
    snapshot = CapabilitySnapshot(
        records=(CapabilityRecord(capability_id="sources.read", healthy=False),)
    )
    _, report = _assess(TruthValue.TRUE, capabilities=snapshot)
    assert report.status is ApplicabilityStatus.CAPABILITY_UNAVAILABLE
    record = snapshot.lookup("sources.read")
    assert record is not None and record.unavailable_reasons() == ("healthy",)


def test_assess_method_reports_a_type_error_before_anything_else() -> None:
    world = World({"ready": TruthValue.TRUE})
    atom = world.atom("ready")
    atom["arguments"] = {"subject": {"op": "constant", "value": 7}}
    contract = method_contract(applicable_when=(atom,))

    report = assess_method(
        task_binding(),
        contract,
        world.snapshot,
        _capabilities("sources.read"),
        registry=world.registry,
    )

    assert report.status is ApplicabilityStatus.TYPE_ERROR
    assert any("wrong type" in message for message in report.type_errors)


def test_a_precondition_may_not_read_a_step_output() -> None:
    world = World({"ready": TruthValue.TRUE})
    atom = world.atom("ready")
    atom["arguments"] = {"subject": {"op": "output", "step": "extract", "port": "evidence"}}
    contract = method_contract(applicable_when=(atom,))

    report = assess_method(
        task_binding(),
        contract,
        world.snapshot,
        _capabilities("sources.read"),
        registry=world.registry,
    )

    assert report.status is ApplicabilityStatus.TYPE_ERROR
    assert any("step output" in message for message in report.type_errors)


def test_a_precondition_argument_may_come_from_a_bound_task_parameter() -> None:
    world = World({"ready": TruthValue.TRUE})
    atom = world.atom("ready")
    atom["arguments"] = {"subject": {"op": "parameter", "name": "subject"}}
    contract = method_contract(applicable_when=(atom,))

    report = assess_method(
        task_binding(parameters={"subject": "ready"}),
        contract,
        world.snapshot,
        _capabilities("sources.read"),
        registry=world.registry,
    )

    assert report.status is ApplicabilityStatus.APPLICABLE


def test_assess_method_has_no_side_effects_on_the_snapshot() -> None:
    world, _ = _assess(TruthValue.TRUE)
    before = world.snapshot.to_json()
    assess_method(
        task_binding(),
        method_contract(applicable_when=(world.atom("ready"),)),
        world.snapshot,
        _capabilities("sources.read"),
        registry=world.registry,
    )
    assert world.snapshot.to_json() == before


# --------------------------------------------------------------------------------------
# Re-check: a moved world invalidates the instance, it does not become a CONFLICT
# --------------------------------------------------------------------------------------


def _draft_for(contract: Any, witness_truth: TruthValue) -> MethodInstanceDraft:
    return MethodInstanceDraft(
        instance_id="instance-1",  # type: ignore[arg-type]
        goal_id=TaskRef("task-1"),
        obligation_id=ObligationId("obligation-1"),
        method_ref=contract.method_ref(),
        precondition_witnesses=(
            PreconditionWitnessRecord(
                condition_digest=condition_digest(contract.applicable_when[0]),
                phase=PreconditionPhase.SELECT,
                truth=witness_truth,
            ),
        ),
    )


def test_a_witness_that_still_holds_is_consistent() -> None:
    world = World({"ready": TruthValue.TRUE})
    contract = method_contract(applicable_when=(world.atom("ready"),))
    result = recheck_method_instance(
        _draft_for(contract, TruthValue.TRUE),
        contract,
        world.snapshot,
        registry=world.registry,
    )
    assert result.status is RecheckStatus.CONSISTENT
    assert result.consistent is True


def test_a_witness_that_no_longer_holds_invalidates_the_method_instance() -> None:
    """§6.6 rule 3: a return code, not a CONFLICT and not a global re-plan."""

    world = World({"ready": TruthValue.FALSE})
    contract = method_contract(applicable_when=(world.atom("ready"),))
    result = recheck_method_instance(
        _draft_for(contract, TruthValue.TRUE),
        contract,
        world.snapshot,
        registry=world.registry,
    )
    assert result.status is RecheckStatus.METHOD_INSTANCE_INVALIDATED
    assert result.changed_digests == (condition_digest(contract.applicable_when[0]),)
    assert "CONFLICT" not in result.reason.upper()


# --------------------------------------------------------------------------------------
# Contract round 3: a closed-world denial must name a registered observer (P1.1c)
# --------------------------------------------------------------------------------------


def _denial(*, observer_id: str | None, coverage: QueryCompleteness) -> ObservationRecord:
    return ObservationRecord(
        observation_id="observation-1",
        proposition_key="proposition-1",
        polarity=False,
        source_ref=tref(TypedRefKind.OBSERVATION, "source-1"),
        observed_at_ms=1_000,
        recorded_at_ms=1_000,
        coverage=coverage,
        coverage_scope="workspace-1",
        query_watermark_ms=900,
        observer_id=observer_id,
    )


def test_a_closed_world_denial_from_a_registered_observer_is_accepted() -> None:
    world = World({}, closed=frozenset({"p"}))
    signature = world.signatures["p"]
    assert signature.observer_ids == ("observer-1",)

    observation = _denial(
        observer_id="observer-1", coverage=QueryCompleteness.AUTHORITATIVE_WITH_SCOPE
    )

    assert observation.is_authoritative_negative is True
    assert authoritative_negative_matches_observer(signature, observation) is True
    assert observation.support.f == 1


def test_a_denial_from_an_observer_the_predicate_does_not_list_is_not_authoritative() -> None:
    """Complete coverage is a claim; whose query it was is the part the registry knows."""

    world = World({}, closed=frozenset({"p"}))
    observation = _denial(
        observer_id="observer-elsewhere", coverage=QueryCompleteness.AUTHORITATIVE_WITH_SCOPE
    )

    assert authoritative_negative_matches_observer(world.signatures["p"], observation) is False


def test_a_denial_that_names_no_observer_is_not_authoritative() -> None:
    world = World({}, closed=frozenset({"p"}))
    observation = _denial(observer_id=None, coverage=QueryCompleteness.AUTHORITATIVE_WITH_SCOPE)

    assert authoritative_negative_matches_observer(world.signatures["p"], observation) is False


def test_a_best_effort_denial_is_not_authoritative_however_it_is_signed() -> None:
    world = World({}, closed=frozenset({"p"}))
    observation = _denial(observer_id="observer-1", coverage=QueryCompleteness.BEST_EFFORT)

    assert observation.is_authoritative_negative is False
    assert authoritative_negative_matches_observer(world.signatures["p"], observation) is False
    assert observation.support.f == 0


def test_an_open_world_predicate_never_gets_a_closed_world_denial() -> None:
    world = World({}, declared_only=frozenset({"p"}))
    observation = _denial(
        observer_id="observer-1", coverage=QueryCompleteness.AUTHORITATIVE_WITH_SCOPE
    )

    assert authoritative_negative_matches_observer(world.signatures["p"], observation) is False


def test_an_observation_round_trips_with_its_observer() -> None:
    observation = _denial(
        observer_id="observer-1", coverage=QueryCompleteness.AUTHORITATIVE_WITH_SCOPE
    )
    assert ObservationRecord.from_json(observation.to_json()) == observation


# --------------------------------------------------------------------------------------
# Contract round 5: a predicate declaration travels through its own codec (P2.1)
# --------------------------------------------------------------------------------------


def test_a_predicate_signature_round_trips_through_its_codec() -> None:
    signature = PredicateSignature(
        predicate_ref=VersionedRef(id="pred-sources", version=2, content_hash="a" * 64),
        parameters=(
            PredicateParameter(name="subject", type=ArgumentType.STRING),
            PredicateParameter(name="depth", type=ArgumentType.INTEGER, required=False),
        ),
        world_assumption=WorldAssumption.CLOSED,
        observer_ids=("observer-1",),
        authority_scope="workspace-1",
        statement="the named sources are readable",
    )

    restored = PredicateSignature.from_json(signature.to_json())

    assert restored == signature
    assert restored.to_json() == signature.to_json()


def test_a_stored_declaration_cannot_acquire_denial_powers_registration_refused() -> None:
    """A CLOSED-world predicate still needs an observer on the way back in."""

    payload = PredicateSignature(
        predicate_ref=VersionedRef(id="pred-sources", version=1, content_hash="a" * 64),
        world_assumption=WorldAssumption.CLOSED,
        observer_ids=("observer-1",),
    ).to_json()
    payload["observer_ids"] = []

    with pytest.raises(ContractError, match="needs at least one authoritative observer"):
        PredicateSignature.from_json(payload)


def test_a_stored_declaration_cannot_smuggle_executable_content_back_in() -> None:
    payload = PredicateSignature(
        predicate_ref=VersionedRef(id="pred-sources", version=1, content_hash="a" * 64),
        statement="the named sources are readable",
    ).to_json()
    payload["statement"] = "eval('True')"

    with pytest.raises(ContractError, match="executable code or a query fragment"):
        PredicateSignature.from_json(payload)


def test_a_predicate_signature_refuses_an_unknown_field() -> None:
    payload = PredicateSignature(
        predicate_ref=VersionedRef(id="pred-sources", version=1, content_hash="a" * 64),
    ).to_json()
    payload["trusted"] = True

    with pytest.raises(ContractError, match="unknown fields"):
        PredicateSignature.from_json(payload)


def test_a_round_tripped_signature_still_resolves_in_the_registry() -> None:
    world = World({"p": TruthValue.TRUE})
    restored = PredicateSignature.from_json(world.signatures["p"].to_json())

    registry = PredicateRegistry()
    registry.register(restored)

    assert registry.resolve(restored.predicate_ref) == restored
