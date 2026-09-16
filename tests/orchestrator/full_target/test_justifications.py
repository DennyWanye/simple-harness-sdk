# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""FULL-TARGET-1.4 P1.1c: justification maintenance (AER group K, pure part).

The scenarios are AER-K01 … K11 read as unit tests of
``agent_orchestrator.knowledge.justifications``.  Everything here is in-memory:
no store, no epoch barrier, no dirty queue (those are P3.5 / P3.6).
"""

from __future__ import annotations

import pytest
from full_target_world import HASH_A, HASH_B, World, tref

from agent_orchestrator.contracts.evidence_state import (
    Availability,
    ObservationRecord,
    QueryCompleteness,
    RecheckOutcome,
    TemporalUse,
    TruthValue,
    Validity,
    WitnessPurpose,
)
from agent_orchestrator.contracts.models import ContractError
from agent_orchestrator.contracts.semantic_base import (
    EvidenceRef,
    EvidenceRefKind,
    TypedRef,
    TypedRefKind,
)
from agent_orchestrator.knowledge.justifications import (
    CONFLICTING_EVIDENCE,
    EVALUATION_INCOMPLETE,
    REFUTED,
    UNKNOWN_EVIDENCE,
    UNVERIFIED_ASSUMPTION,
    Anchor,
    AnchorCandidate,
    AnchorOrigin,
    AnchorRejection,
    AnchorSelector,
    Atom,
    ClosureStatus,
    ConsumerUse,
    EvidencePremise,
    JustificationSet,
    LineageRecord,
    Polarity,
    PropositionPremise,
    RuleCondition,
    SupportGraph,
    SupportWitness,
    WitnessKind,
    grounded_closure,
    parse_polarity,
    reevaluate_consumer,
)
from agent_orchestrator.knowledge.predicates import WorldAssumption

SCOPE = "mission-1"
AS_OF = 1_000


# --------------------------------------------------------------------------- #
# builders
# --------------------------------------------------------------------------- #


def observation(
    key: str,
    *,
    name: str,
    polarity: bool = True,
    authoritative: bool = False,
    observed_at_ms: int = 500,
    valid_from_ms: int | None = None,
    valid_until_ms: int | None = None,
    coverage_scope: str | None = SCOPE,
) -> ObservationRecord:
    """A real observation record; negatives only deny when authoritative."""

    coverage = (
        QueryCompleteness.AUTHORITATIVE_WITH_SCOPE
        if authoritative
        else QueryCompleteness.BEST_EFFORT
    )
    return ObservationRecord(
        observation_id=f"obs-{name}",
        proposition_key=key,
        polarity=polarity,
        source_ref=tref(TypedRefKind.OBSERVATION, f"src-{name}"),
        observed_at_ms=observed_at_ms,
        recorded_at_ms=observed_at_ms,
        coverage=coverage,
        coverage_scope=coverage_scope if authoritative else None,
        query_watermark_ms=observed_at_ms if authoritative else None,
        valid_from_ms=valid_from_ms,
        valid_until_ms=valid_until_ms,
    )


def evidence_ref(name: str, *, revision: int = 1) -> EvidenceRef:
    return EvidenceRef(
        kind=EvidenceRefKind.OBSERVATION,
        id=f"evidence-{name}",
        revision=revision,
        content_hash=HASH_A,
    )


def candidate(
    key: str,
    *,
    name: str,
    group: str = "group-1",
    polarity: bool = True,
    authoritative: bool = False,
    with_ref: bool = True,
    **kwargs: object,
) -> AnchorCandidate:
    observation_kwargs = {
        field: kwargs.pop(field)
        for field in ("observed_at_ms", "valid_from_ms", "valid_until_ms", "coverage_scope")
        if field in kwargs
    }
    return AnchorCandidate(
        observation=observation(
            key,
            name=name,
            polarity=polarity,
            authoritative=authoritative,
            **observation_kwargs,  # type: ignore[arg-type]
        ),
        scope_id=str(kwargs.pop("scope_id", SCOPE)),
        source_group=group,
        evidence_ref=evidence_ref(name) if with_ref else None,
        **kwargs,  # type: ignore[arg-type]
    )


def selector(
    world: World | None = None,
    *,
    purpose: WitnessPurpose = WitnessPurpose.ACCEPT,
    as_of_ms: int = AS_OF,
    scope_id: str = SCOPE,
) -> AnchorSelector:
    signatures = (
        {} if world is None else {world.keys[name]: world.signatures[name] for name in world.keys}
    )
    return AnchorSelector(
        scope_id=scope_id, purpose=purpose, as_of_ms=as_of_ms, signatures=signatures
    )


def rule(
    conclusion: str,
    *premises: object,
    polarity: Polarity = Polarity.POSITIVE,
    version: str = "rule-v1",
    group: str | None = None,
    conditions: tuple[RuleCondition, ...] = (),
) -> JustificationSet:
    return JustificationSet(
        conclusion=conclusion,
        polarity=polarity,
        premises=tuple(premises),
        conditions=conditions,
        rule_version=version,
        source_group=group,
    )


def anchors_for(world: World, *candidates: AnchorCandidate, **kwargs: object) -> tuple[Anchor, ...]:
    return selector(world, **kwargs).select(candidates).anchors  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# AER-K01 — unknown is not false
# --------------------------------------------------------------------------- #


def test_k01_no_record_is_unknown_not_false() -> None:
    world = World({}, declared_only=frozenset({"paid"}))
    result = grounded_closure(SupportGraph(), ())
    assert result.truth_for(world.keys["paid"]) is TruthValue.UNKNOWN
    assert result.support_for(world.keys["paid"]).f == 0


def test_k01_best_effort_negative_is_not_a_denial() -> None:
    world = World({}, declared_only=frozenset({"paid"}))
    key = world.keys["paid"]
    selection = selector(world).select(
        [candidate(key, name="lookup", polarity=False, authoritative=False)]
    )
    assert selection.anchors == ()
    assert selection.reason_for("obs-lookup") is AnchorRejection.NOT_AUTHORITATIVE_NEGATIVE
    assert grounded_closure(SupportGraph(), selection.anchors).truth_for(key) is TruthValue.UNKNOWN


def test_k01_closed_domain_without_record_stays_unknown() -> None:
    world = World({}, closed=frozenset({"paid"}))
    assert world.signatures["paid"].world_assumption is WorldAssumption.CLOSED
    result = grounded_closure(SupportGraph(), ())
    assert result.truth_for(world.keys["paid"]) is TruthValue.UNKNOWN


def test_k01_closed_domain_authoritative_negative_denies() -> None:
    world = World({}, closed=frozenset({"paid"}))
    key = world.keys["paid"]
    selection = selector(world).select(
        [
            candidate(
                key,
                name="registry",
                polarity=False,
                authoritative=True,
                observer_id="observer-1",
            )
        ]
    )
    assert len(selection.anchors) == 1
    result = grounded_closure(SupportGraph(), selection.anchors)
    assert result.truth_for(key) is TruthValue.FALSE


def test_k01_closed_domain_negative_needs_matching_coverage_scope() -> None:
    world = World({}, closed=frozenset({"paid"}))
    key = world.keys["paid"]
    selection = selector(world).select(
        [
            candidate(
                key,
                name="registry",
                polarity=False,
                authoritative=True,
                observer_id="observer-1",
                coverage_scope="other-mission",
            )
        ]
    )
    assert selection.reason_for("obs-registry") is AnchorRejection.COVERAGE_SCOPE_MISMATCH
    assert grounded_closure(SupportGraph(), selection.anchors).truth_for(key) is TruthValue.UNKNOWN


def test_k01_closed_domain_negative_needs_an_authoritative_observer() -> None:
    world = World({}, closed=frozenset({"paid"}))
    key = world.keys["paid"]
    selection = selector(world).select(
        [
            candidate(
                key,
                name="rumour",
                polarity=False,
                authoritative=True,
                observer_id="observer-unknown",
            )
        ]
    )
    assert selection.reason_for("obs-rumour") is AnchorRejection.OBSERVER_NOT_AUTHORITATIVE
    assert grounded_closure(SupportGraph(), selection.anchors).truth_for(key) is TruthValue.UNKNOWN


def test_k01_open_domain_counter_observation_is_a_legitimate_false() -> None:
    world = World({}, declared_only=frozenset({"paid"}))
    key = world.keys["paid"]
    anchors = anchors_for(world, candidate(key, name="audit", polarity=False, authoritative=True))
    assert grounded_closure(SupportGraph(), anchors).truth_for(key) is TruthValue.FALSE


def test_k01_unregistered_predicate_is_not_an_anchor() -> None:
    world = World({}, declared_only=frozenset({"paid"}))
    selection = selector(world).select([candidate("pred-ghost@1#deadbeef", name="ghost")])
    assert selection.reason_for("obs-ghost") is AnchorRejection.UNREGISTERED_PREDICATE


# --------------------------------------------------------------------------- #
# AER-K02 — multiple support groups
# --------------------------------------------------------------------------- #


def _k02_world() -> tuple[World, SupportGraph]:
    world = World({}, declared_only=frozenset({"k", "a1"}))
    graph = SupportGraph(
        [
            rule(
                world.keys["k"],
                evidence_ref("e1"),
                Atom(world.keys["a1"]),
                version="rule-k-group1",
            ),
            rule(world.keys["k"], evidence_ref("e2"), version="rule-k-group2"),
            rule(world.keys["a1"], evidence_ref("e1"), version="rule-a1"),
        ]
    )
    return world, graph


def test_k02_both_groups_hold_and_each_leaves_its_own_witness() -> None:
    world, graph = _k02_world()
    anchors = anchors_for(
        world,
        candidate(world.keys["a1"], name="e1", group="group-1"),
        candidate(world.keys["k"], name="e2", group="group-2"),
    )
    result = grounded_closure(graph, anchors)
    assert result.truth_for(world.keys["k"]) is TruthValue.TRUE
    versions = {
        witness.rule_version
        for witness in result.witnesses_for(Atom(world.keys["k"]))
        if witness.kind is WitnessKind.DERIVED
    }
    assert versions == {"rule-k-group1", "rule-k-group2"}


def test_k02_withdrawing_one_group_leaves_the_other_sufficient() -> None:
    world, graph = _k02_world()
    anchors = anchors_for(world, candidate(world.keys["k"], name="e2", group="group-2"))
    result = grounded_closure(graph, anchors)
    assert result.truth_for(world.keys["k"]) is TruthValue.TRUE
    assert result.truth_for(world.keys["a1"]) is TruthValue.UNKNOWN


def test_k02_withdrawing_every_group_removes_support() -> None:
    _world, graph = _k02_world()
    result = grounded_closure(graph, ())
    assert result.witnesses == {}
    assert result.admitted_signatures == frozenset()


def test_k02_the_surviving_witness_names_the_group_that_fired() -> None:
    world, graph = _k02_world()
    anchors = anchors_for(world, candidate(world.keys["k"], name="e2", group="group-2"))
    witnesses = grounded_closure(graph, anchors).witnesses_for(Atom(world.keys["k"]))
    derived = [witness for witness in witnesses if witness.kind is WitnessKind.DERIVED]
    assert len(derived) == 1
    assert derived[0].rule_version == "rule-k-group2"
    assert derived[0].premises == (EvidencePremise(ref=evidence_ref("e2")),)


def test_k02_a_premise_pinned_to_a_revision_is_not_met_by_another_revision() -> None:
    world = World({}, declared_only=frozenset({"k", "a1"}))
    graph = SupportGraph([rule(world.keys["k"], evidence_ref("e1", revision=3))])
    anchors = anchors_for(world, candidate(world.keys["a1"], name="e1"))
    assert anchors[0].evidence_ref == evidence_ref("e1", revision=1)
    assert grounded_closure(graph, anchors).truth_for(world.keys["k"]) is TruthValue.UNKNOWN


# --------------------------------------------------------------------------- #
# AER-K03 — source dependence: counting is not independence
# --------------------------------------------------------------------------- #


def test_k03_ten_restatements_of_one_source_are_one_group() -> None:
    world = World({}, declared_only=frozenset({"claim"}))
    key = world.keys["claim"]
    anchors = anchors_for(
        world,
        *(candidate(key, name=f"copy{n}", group="wire-service") for n in range(10)),
    )
    result = grounded_closure(SupportGraph(), anchors)
    assert result.support_for(key).t == 10
    assert result.independent_source_groups(key) == frozenset({"wire-service"})
    assert not result.satisfies_independence(key, required=2)


def test_k03_two_genuinely_independent_sources_satisfy_the_policy() -> None:
    world = World({}, declared_only=frozenset({"claim"}))
    key = world.keys["claim"]
    anchors = anchors_for(
        world,
        candidate(key, name="lab", group="lab-a"),
        candidate(key, name="field", group="field-b"),
    )
    result = grounded_closure(SupportGraph(), anchors)
    assert result.satisfies_independence(key, required=2)


def test_k03_a_derived_conclusion_inherits_its_premises_source_groups() -> None:
    world = World({}, declared_only=frozenset({"a", "b"}))
    graph = SupportGraph([rule(world.keys["b"], Atom(world.keys["a"]))])
    anchors = anchors_for(world, candidate(world.keys["a"], name="lab", group="lab-a"))
    result = grounded_closure(graph, anchors)
    assert result.independent_source_groups(world.keys["b"]) == frozenset({"lab-a"})
    assert not result.satisfies_independence(world.keys["b"], required=2)


def test_k03_the_same_group_offered_twice_is_still_one_group() -> None:
    world = World({}, declared_only=frozenset({"k"}))
    duplicate = rule(world.keys["k"], evidence_ref("e1"))
    graph = SupportGraph([duplicate, duplicate])
    assert len(graph) == 1


# --------------------------------------------------------------------------- #
# AER-K04 — mutual reference without an anchor
# --------------------------------------------------------------------------- #


def test_k04_mutual_reference_without_an_anchor_has_no_support() -> None:
    world = World({}, declared_only=frozenset({"a", "b"}))
    graph = SupportGraph(
        [
            rule(world.keys["a"], Atom(world.keys["b"]), version="rule-a-from-b"),
            rule(world.keys["b"], Atom(world.keys["a"]), version="rule-b-from-a"),
        ]
    )
    result = grounded_closure(graph, ())
    assert result.truth_for(world.keys["a"]) is TruthValue.UNKNOWN
    assert result.truth_for(world.keys["b"]) is TruthValue.UNKNOWN


def test_k04_an_unanchored_cycle_admits_no_justification_signature() -> None:
    world = World({}, declared_only=frozenset({"a", "b"}))
    graph = SupportGraph(
        [
            rule(world.keys["a"], Atom(world.keys["b"]), version="rule-a-from-b"),
            rule(world.keys["b"], Atom(world.keys["a"]), version="rule-b-from-a"),
        ]
    )
    result = grounded_closure(graph, ())
    assert result.admitted_signatures == frozenset()
    assert result.node_count == 0
    assert result.is_complete


def test_k04_an_unanchored_cycle_blocks_release_as_unknown_not_as_refuted() -> None:
    world = World({}, declared_only=frozenset({"a", "b"}))
    graph = SupportGraph([rule(world.keys["a"], Atom(world.keys["b"]))])
    result = grounded_closure(graph, ())
    assert result.release_block_reason(world.keys["a"], high_risk=False) == UNKNOWN_EVIDENCE


# --------------------------------------------------------------------------- #
# AER-K05 — a cycle with an external premise that is still insufficient
# --------------------------------------------------------------------------- #


def test_k05_a_rule_needing_its_own_conclusion_cannot_bootstrap() -> None:
    world = World({}, declared_only=frozenset({"a", "e"}))
    graph = SupportGraph([rule(world.keys["a"], Atom(world.keys["a"]), Atom(world.keys["e"]))])
    anchors = anchors_for(world, candidate(world.keys["e"], name="e"))
    result = grounded_closure(graph, anchors)
    assert result.truth_for(world.keys["e"]) is TruthValue.TRUE
    assert result.truth_for(world.keys["a"]) is TruthValue.UNKNOWN


def test_k05_having_an_incoming_edge_from_outside_the_scc_is_not_enough() -> None:
    world = World({}, declared_only=frozenset({"a", "b", "e"}))
    graph = SupportGraph(
        [
            rule(world.keys["a"], Atom(world.keys["b"]), Atom(world.keys["e"])),
            rule(world.keys["b"], Atom(world.keys["a"])),
        ]
    )
    anchors = anchors_for(world, candidate(world.keys["e"], name="e"))
    result = grounded_closure(graph, anchors)
    assert result.truth_for(world.keys["a"]) is TruthValue.UNKNOWN
    assert result.truth_for(world.keys["b"]) is TruthValue.UNKNOWN


def test_k05_the_same_cycle_is_derivable_once_a_real_entry_rule_exists() -> None:
    world = World({}, declared_only=frozenset({"a", "b", "e"}))
    graph = SupportGraph(
        [
            rule(world.keys["a"], Atom(world.keys["e"]), version="rule-a-from-e"),
            rule(world.keys["b"], Atom(world.keys["a"]), version="rule-b-from-a"),
            rule(world.keys["a"], Atom(world.keys["b"]), version="rule-a-from-b"),
        ]
    )
    anchors = anchors_for(world, candidate(world.keys["e"], name="e"))
    result = grounded_closure(graph, anchors)
    assert result.truth_for(world.keys["a"]) is TruthValue.TRUE
    assert result.truth_for(world.keys["b"]) is TruthValue.TRUE


# --------------------------------------------------------------------------- #
# AER-K06 — anchor withdrawal propagates
# --------------------------------------------------------------------------- #


def _k06_graph(world: World) -> SupportGraph:
    return SupportGraph(
        [
            rule(world.keys["a"], Atom(world.keys["e"]), version="rule-a-from-e"),
            rule(world.keys["b"], Atom(world.keys["a"]), version="rule-b-from-a"),
            rule(world.keys["a"], Atom(world.keys["b"]), version="rule-a-from-b"),
        ]
    )


def test_k06_withdrawing_the_anchor_extinguishes_the_whole_cycle() -> None:
    world = World({}, declared_only=frozenset({"a", "b", "e"}))
    graph = _k06_graph(world)
    before = grounded_closure(graph, anchors_for(world, candidate(world.keys["e"], name="e")))
    assert before.truth_for(world.keys["b"]) is TruthValue.TRUE

    after = grounded_closure(graph, ())
    assert after.truth_for(world.keys["a"]) is TruthValue.UNKNOWN
    assert after.truth_for(world.keys["b"]) is TruthValue.UNKNOWN


def test_k06_a_revoked_anchor_is_no_longer_selected() -> None:
    world = World({}, declared_only=frozenset({"a", "b", "e"}))
    graph = _k06_graph(world)
    selection = selector(world).select(
        [candidate(world.keys["e"], name="e", validity=Validity.REVOKED)]
    )
    assert selection.reason_for("obs-e") is AnchorRejection.NOT_CURRENT
    assert grounded_closure(graph, selection.anchors).truth_for(world.keys["b"]) is (
        TruthValue.UNKNOWN
    )


def test_k06_a_derived_support_may_not_be_fed_back_as_an_anchor() -> None:
    world = World({}, declared_only=frozenset({"a", "e"}))
    graph = _k06_graph(World({}, declared_only=frozenset({"a", "b", "e"})))
    previous = grounded_closure(graph, anchors_for(world, candidate(world.keys["e"], name="e")))
    witness = previous.witnesses_for(Atom(world.keys["a"]))[0]
    assert isinstance(witness, SupportWitness)
    with pytest.raises(ContractError, match="not an anchor"):
        grounded_closure(graph, [witness])  # type: ignore[list-item]


def test_k06_a_plain_atom_is_not_accepted_as_an_anchor_either() -> None:
    world = World({}, declared_only=frozenset({"e"}))
    with pytest.raises(ContractError, match="AnchorSelector output"):
        grounded_closure(SupportGraph(), [Atom(world.keys["e"])])  # type: ignore[list-item]


# --------------------------------------------------------------------------- #
# AER-K07 — a counter-observation is not diluted
# --------------------------------------------------------------------------- #


def _k07_selection(world: World, *, positives: int = 3) -> tuple[Anchor, ...]:
    key = world.keys["safe"]
    return anchors_for(
        world,
        *(candidate(key, name=f"pos{n}", group=f"group-{n}") for n in range(positives)),
        candidate(
            key,
            name="counter",
            polarity=False,
            authoritative=True,
            group="audit",
            observer_id="observer-1",
        ),
    )


def test_k07_three_positives_and_one_denial_are_a_conflict() -> None:
    world = World({}, closed=frozenset({"safe"}))
    result = grounded_closure(SupportGraph(), _k07_selection(world))
    support = result.support_for(world.keys["safe"])
    assert (support.t, support.f) == (3, 1)
    assert result.truth_for(world.keys["safe"]) is TruthValue.CONFLICT


def test_k07_conflict_blocks_release_rather_than_taking_a_majority() -> None:
    world = World({}, closed=frozenset({"safe"}))
    result = grounded_closure(SupportGraph(), _k07_selection(world))
    assert not result.may_release(world.keys["safe"], high_risk=False)
    assert result.release_block_reason(world.keys["safe"], high_risk=False) == (
        CONFLICTING_EVIDENCE
    )


def test_k07_adding_a_new_sufficient_support_does_not_clear_a_live_denial() -> None:
    world = World({}, closed=frozenset({"safe", "extra"}))
    graph = SupportGraph([rule(world.keys["safe"], Atom(world.keys["extra"]))])
    anchors = (
        *_k07_selection(world),
        *anchors_for(world, candidate(world.keys["extra"], name="extra", group="new-lab")),
    )
    result = grounded_closure(graph, anchors)
    assert result.truth_for(world.keys["safe"]) is TruthValue.CONFLICT


def test_k07_conflict_leaves_only_once_the_denial_is_retracted_with_a_receipt() -> None:
    world = World({}, closed=frozenset({"safe"}))
    key = world.keys["safe"]
    retracted = candidate(
        key,
        name="counter",
        polarity=False,
        authoritative=True,
        group="audit",
        observer_id="observer-1",
        inapplicable=True,
        inapplicable_receipt=tref(TypedRefKind.REVIEW, "review-retraction"),
    )
    selection = selector(world).select([candidate(key, name="pos0", group="group-0"), retracted])
    assert selection.reason_for("obs-counter") is AnchorRejection.SUPERSEDED_BY_RECEIPT
    assert grounded_closure(SupportGraph(), selection.anchors).truth_for(key) is TruthValue.TRUE


def test_k07_declaring_a_denial_inapplicable_requires_an_explicit_receipt() -> None:
    world = World({}, closed=frozenset({"safe"}))
    with pytest.raises(ContractError, match="explicit receipt"):
        candidate(
            world.keys["safe"],
            name="counter",
            polarity=False,
            authoritative=True,
            inapplicable=True,
        )


def test_k07_asking_for_the_negative_side_of_a_true_proposition_is_refuted() -> None:
    world = World({}, declared_only=frozenset({"safe"}))
    anchors = anchors_for(world, candidate(world.keys["safe"], name="pos"))
    result = grounded_closure(SupportGraph(), anchors)
    assert (
        result.release_block_reason(world.keys["safe"], high_risk=False, polarity=Polarity.NEGATIVE)
        == REFUTED
    )


# --------------------------------------------------------------------------- #
# AER-K10 (pure part) — HISTORICAL_AS_OF vs CURRENT_AT_USE anchor selection
# --------------------------------------------------------------------------- #


def test_k10_current_at_use_evidence_expires_at_the_gate() -> None:
    world = World({}, declared_only=frozenset({"stock"}))
    selection = selector(world, purpose=WitnessPurpose.ACCEPT).select(
        [candidate(world.keys["stock"], name="count", valid_until_ms=800)]
    )
    assert selection.reason_for("obs-count") is AnchorRejection.EXPIRED


def test_k10_historical_evidence_still_describes_the_past_for_a_context_read() -> None:
    world = World({}, declared_only=frozenset({"stock"}))
    selection = selector(world, purpose=WitnessPurpose.CONTEXT).select(
        [
            candidate(
                world.keys["stock"],
                name="count",
                valid_until_ms=800,
                temporal_use=TemporalUse.HISTORICAL_AS_OF,
            )
        ]
    )
    assert len(selection.anchors) == 1
    assert selection.anchors[0].historical is True


def test_k10_historical_evidence_does_not_pass_an_accept_gate() -> None:
    world = World({}, declared_only=frozenset({"stock"}))
    selection = selector(world, purpose=WitnessPurpose.ACCEPT).select(
        [
            candidate(
                world.keys["stock"],
                name="count",
                valid_until_ms=800,
                temporal_use=TemporalUse.HISTORICAL_AS_OF,
            )
        ]
    )
    assert selection.reason_for("obs-count") is AnchorRejection.EXPIRED


def test_k10_a_historical_purpose_does_not_exempt_permission() -> None:
    world = World({}, declared_only=frozenset({"stock"}))
    selection = selector(world, purpose=WitnessPurpose.CONTEXT).select(
        [
            candidate(
                world.keys["stock"],
                name="count",
                valid_until_ms=800,
                temporal_use=TemporalUse.HISTORICAL_AS_OF,
                availability=Availability.REDACTED,
            )
        ]
    )
    assert selection.reason_for("obs-count") is AnchorRejection.UNREADABLE


def test_k10_an_observation_made_after_as_of_is_not_an_anchor() -> None:
    world = World({}, declared_only=frozenset({"stock"}))
    selection = selector(world).select(
        [candidate(world.keys["stock"], name="future", observed_at_ms=5_000)]
    )
    assert selection.reason_for("obs-future") is AnchorRejection.AFTER_AS_OF


def test_k10_a_continuous_premise_needs_a_declared_monitor_interval() -> None:
    world = World({}, declared_only=frozenset({"stock"}))
    unmonitored = candidate(world.keys["stock"], name="lock", temporal_use=TemporalUse.CONTINUOUS)
    blocked = selector(world, purpose=WitnessPurpose.MAINTAIN).select([unmonitored])
    assert blocked.reason_for("obs-lock") is AnchorRejection.NO_CONTINUOUS_GUARANTEE

    monitored = candidate(
        world.keys["stock"],
        name="lock",
        temporal_use=TemporalUse.CONTINUOUS,
        monitor_interval_ms=250,
    )
    assert len(selector(world, purpose=WitnessPurpose.MAINTAIN).select([monitored]).anchors) == 1


def test_k10_evidence_from_another_scope_is_not_selected() -> None:
    world = World({}, declared_only=frozenset({"stock"}))
    selection = selector(world).select(
        [candidate(world.keys["stock"], name="elsewhere", scope_id="mission-2")]
    )
    assert selection.reason_for("obs-elsewhere") is AnchorRejection.SCOPE_MISMATCH


def test_k10_an_untraceable_report_is_not_an_anchor() -> None:
    world = World({}, declared_only=frozenset({"stock"}))
    selection = selector(world).select(
        [candidate(world.keys["stock"], name="hearsay", traceable=False)]
    )
    assert selection.reason_for("obs-hearsay") is AnchorRejection.NOT_TRACEABLE


def test_k10_a_predicted_effect_is_never_an_observation() -> None:
    world = World({}, declared_only=frozenset({"stock"}))
    selection = selector(world).select(
        [candidate(world.keys["stock"], name="expected", predicted=True)]
    )
    assert selection.reason_for("obs-expected") is AnchorRejection.PREDICTED_NOT_OBSERVED


# --------------------------------------------------------------------------- #
# AER-K11 — was_used and supports_for_use cannot rewrite each other
# --------------------------------------------------------------------------- #


def lineage() -> LineageRecord:
    return LineageRecord(
        tref(TypedRefKind.ARTIFACT, "report-1"),
        (evidence_ref("e1"),),
        ("signature-old",),
    )


def test_k11_rebinding_supports_leaves_the_literal_citation_alone() -> None:
    record = lineage().with_supports(("signature-e2",))
    assert record.supports_for_use == ("signature-e2",)
    assert record.was_used == (evidence_ref("e1"),)
    assert record.cites(evidence_ref("e1"))


def test_k11_recording_a_new_read_leaves_the_current_reasons_alone() -> None:
    record = lineage().with_recorded_use(evidence_ref("e2"))
    assert record.was_used == (evidence_ref("e1"), evidence_ref("e2"))
    assert record.supports_for_use == ("signature-old",)


def test_k11_history_cannot_be_adopted_as_a_current_reason() -> None:
    with pytest.raises(ContractError, match="historical lineage"):
        lineage().adopt_history_as_support()


def test_k11_a_current_reason_cannot_rewrite_history() -> None:
    with pytest.raises(ContractError, match="must not rewrite was_used"):
        lineage().rewrite_history_from_supports()


def test_k11_lineage_fields_are_not_assignable() -> None:
    record = lineage()
    with pytest.raises(ContractError, match="immutable"):
        record._was_used = ()  # type: ignore[misc]


def test_k11_withdrawn_citations_are_reported_against_the_admitted_set() -> None:
    record = lineage().with_recorded_use(evidence_ref("e2"))
    assert record.withdrawn_citations([evidence_ref("e2")]) == (evidence_ref("e1"),)
    assert record.withdrawn_citations([evidence_ref("e1"), evidence_ref("e2")]) == ()


# --------------------------------------------------------------------------- #
# EVALUATION_INCOMPLETE blocks release
# --------------------------------------------------------------------------- #


def test_budget_exhaustion_reports_evaluation_incomplete() -> None:
    world = World({}, declared_only=frozenset({"a", "b"}))
    anchors = anchors_for(
        world,
        candidate(world.keys["a"], name="a"),
        candidate(world.keys["b"], name="b"),
    )
    result = grounded_closure(SupportGraph(), anchors, node_budget=1)
    assert result.status is ClosureStatus.EVALUATION_INCOMPLETE
    assert result.node_count == 1


def test_evaluation_incomplete_blocks_release_of_an_already_supported_atom() -> None:
    world = World({}, declared_only=frozenset({"a", "b"}))
    anchors = anchors_for(
        world,
        candidate(world.keys["a"], name="a"),
        candidate(world.keys["b"], name="b"),
    )
    result = grounded_closure(SupportGraph(), anchors, node_budget=1)
    supported = result.witnesses_for(Atom(world.keys["a"]))
    assert supported, "the first anchor did land before the budget ran out"
    assert not result.may_release(world.keys["a"], high_risk=False)
    assert result.release_block_reason(world.keys["a"], high_risk=False) == (EVALUATION_INCOMPLETE)


def test_evaluation_incomplete_is_not_reported_as_an_unknown_world_fact() -> None:
    world = World({}, declared_only=frozenset({"a", "b"}))
    anchors = anchors_for(
        world,
        candidate(world.keys["a"], name="a"),
        candidate(world.keys["b"], name="b"),
    )
    result = grounded_closure(SupportGraph(), anchors, node_budget=1)
    assert result.release_block_reason(world.keys["b"], high_risk=False) != UNKNOWN_EVIDENCE
    with pytest.raises(ContractError, match="EVALUATION_INCOMPLETE"):
        result.require_complete()


def test_a_derivation_that_exceeds_the_budget_is_also_incomplete() -> None:
    world = World({}, declared_only=frozenset({"a", "b"}))
    graph = SupportGraph([rule(world.keys["b"], Atom(world.keys["a"]))])
    anchors = anchors_for(world, candidate(world.keys["a"], name="a"))
    result = grounded_closure(graph, anchors, node_budget=1)
    assert result.status is ClosureStatus.EVALUATION_INCOMPLETE
    assert result.truth_for(world.keys["b"]) is TruthValue.UNKNOWN
    assert result.release_block_reason(world.keys["b"], high_risk=False) == (EVALUATION_INCOMPLETE)


def test_a_sufficient_budget_completes_and_releases() -> None:
    world = World({}, declared_only=frozenset({"a", "b"}))
    graph = SupportGraph([rule(world.keys["b"], Atom(world.keys["a"]))])
    anchors = anchors_for(world, candidate(world.keys["a"], name="a"))
    result = grounded_closure(graph, anchors, node_budget=8)
    assert result.is_complete
    assert result.may_release(world.keys["b"], high_risk=False)
    result.require_complete()


def test_the_node_budget_must_be_a_positive_integer() -> None:
    with pytest.raises(ContractError):
        grounded_closure(SupportGraph(), (), node_budget=0)


# --------------------------------------------------------------------------- #
# explicitly revocable assumptions (no negation as failure)
# --------------------------------------------------------------------------- #


def _assumption_graph(world: World) -> SupportGraph:
    return SupportGraph(
        [
            rule(
                world.keys["b"],
                Atom(world.keys["a"]),
                conditions=(RuleCondition(name="no-open-incident", defeasible=True),),
            )
        ]
    )


def test_an_unasserted_condition_is_not_true_by_default() -> None:
    world = World({}, declared_only=frozenset({"a", "b"}))
    anchors = anchors_for(world, candidate(world.keys["a"], name="a"))
    result = grounded_closure(_assumption_graph(world), anchors)
    assert result.truth_for(world.keys["b"]) is TruthValue.UNKNOWN


def test_an_asserted_assumption_supports_but_taints_the_conclusion() -> None:
    world = World({}, declared_only=frozenset({"a", "b"}))
    anchors = anchors_for(world, candidate(world.keys["a"], name="a"))
    result = grounded_closure(
        _assumption_graph(world), anchors, assumptions={"no-open-incident": True}
    )
    assert result.truth_for(world.keys["b"]) is TruthValue.TRUE
    assert result.rests_on_assumption(world.keys["b"])
    assert not result.rests_on_assumption(world.keys["a"])


def test_a_high_risk_gate_refuses_an_unverified_assumption() -> None:
    world = World({}, declared_only=frozenset({"a", "b"}))
    anchors = anchors_for(world, candidate(world.keys["a"], name="a"))
    result = grounded_closure(
        _assumption_graph(world), anchors, assumptions={"no-open-incident": True}
    )
    assert result.may_release(world.keys["b"], high_risk=False)
    assert not result.may_release(world.keys["b"], high_risk=True)
    assert result.release_block_reason(world.keys["b"], high_risk=True) == UNVERIFIED_ASSUMPTION


def test_an_independent_clean_group_untaints_the_conclusion() -> None:
    world = World({}, declared_only=frozenset({"a", "b"}))
    graph = _assumption_graph(world).with_justifications(
        rule(world.keys["b"], Atom(world.keys["a"]), version="rule-b-clean")
    )
    anchors = anchors_for(world, candidate(world.keys["a"], name="a"))
    result = grounded_closure(graph, anchors, assumptions={"no-open-incident": True})
    assert not result.rests_on_assumption(world.keys["b"])
    assert result.may_release(world.keys["b"], high_risk=True)


def test_a_falsified_assumption_blocks_the_rule() -> None:
    world = World({}, declared_only=frozenset({"a", "b"}))
    anchors = anchors_for(world, candidate(world.keys["a"], name="a"))
    result = grounded_closure(
        _assumption_graph(world), anchors, assumptions={"no-open-incident": False}
    )
    assert result.truth_for(world.keys["b"]) is TruthValue.UNKNOWN


# --------------------------------------------------------------------------- #
# §11.5 / AER §10.3 — the five re-evaluation outcomes
# --------------------------------------------------------------------------- #


CONSUMER = TypedRef(kind=TypedRefKind.REVIEW, id="review-1", revision=1, content_hash=HASH_B)


def use(**kwargs: object) -> ConsumerUse:
    base: dict[str, object] = {
        "consumer_ref": CONSUMER,
        "purpose": WitnessPurpose.ACCEPT,
        "conclusion": "pred-safe@1#abc",
        "truth": TruthValue.TRUE,
        "support_signatures": ("signature-e1",),
        "requirements_digest": "requirements-v1",
        "inputs_digest": "inputs-v1",
    }
    base.update(kwargs)
    return ConsumerUse(**base)  # type: ignore[arg-type]


def test_recheck_unchanged_when_the_same_supports_still_hold() -> None:
    assert reevaluate_consumer(use(), use()) is RecheckOutcome.UNCHANGED


def test_recheck_rebound_support_when_only_the_reason_changed() -> None:
    after = use(support_signatures=("signature-e2",))
    assert reevaluate_consumer(use(), after) is RecheckOutcome.REBOUND_SUPPORT


def test_recheck_needs_review_when_a_literal_citation_was_withdrawn() -> None:
    after = use(
        support_signatures=("signature-e2",),
        literal_citations=(evidence_ref("e1"),),
        withdrawn_citations=(evidence_ref("e1"),),
    )
    assert reevaluate_consumer(use(), after) is RecheckOutcome.NEEDS_REVIEW


def test_recheck_rebound_support_when_the_conclusion_is_not_byte_bound() -> None:
    after = use(
        support_signatures=("signature-e2",),
        literal_citations=(evidence_ref("e1"),),
        withdrawn_citations=(evidence_ref("e1"),),
        citation_binding_required=False,
    )
    assert reevaluate_consumer(use(), after) is RecheckOutcome.REBOUND_SUPPORT


def test_recheck_needs_review_when_rebinding_is_not_permitted() -> None:
    after = use(support_signatures=("signature-e2",), rebinding_allowed=False)
    assert reevaluate_consumer(use(), after) is RecheckOutcome.NEEDS_REVIEW


def test_recheck_needs_review_when_the_inputs_actually_changed() -> None:
    after = use(support_signatures=("signature-e2",), inputs_digest="inputs-v2")
    assert reevaluate_consumer(use(), after) is RecheckOutcome.NEEDS_REVIEW


def test_recheck_needs_review_when_support_disappeared() -> None:
    after = use(truth=TruthValue.UNKNOWN, support_signatures=())
    assert reevaluate_consumer(use(), after) is RecheckOutcome.NEEDS_REVIEW


def test_recheck_needs_review_when_the_support_is_no_longer_current() -> None:
    after = use(validity=Validity.STALE)
    assert reevaluate_consumer(use(), after) is RecheckOutcome.NEEDS_REVIEW


def test_recheck_invalid_when_the_premise_was_refuted() -> None:
    after = use(truth=TruthValue.FALSE, support_signatures=())
    assert reevaluate_consumer(use(), after) is RecheckOutcome.INVALID


def test_recheck_invalid_on_a_conflict() -> None:
    after = use(truth=TruthValue.CONFLICT)
    assert reevaluate_consumer(use(), after) is RecheckOutcome.INVALID


def test_recheck_invalid_when_the_requirement_itself_changed() -> None:
    after = use(requirements_digest="requirements-v2")
    assert reevaluate_consumer(use(), after) is RecheckOutcome.INVALID


def test_recheck_unavailable_never_asserts_the_content_is_wrong() -> None:
    after = use(availability=Availability.UNAVAILABLE, truth=TruthValue.UNKNOWN)
    assert reevaluate_consumer(use(), after) is RecheckOutcome.UNAVAILABLE


def test_recheck_unavailable_wins_over_a_missing_requirement_digest() -> None:
    after = use(availability=Availability.REDACTED, requirements_digest="requirements-v2")
    assert reevaluate_consumer(use(), after) is RecheckOutcome.UNAVAILABLE


def test_recheck_refuses_to_compare_two_different_conclusions() -> None:
    with pytest.raises(ContractError, match="one conclusion"):
        reevaluate_consumer(use(), use(conclusion="pred-other@1#abc"))


def test_recheck_refuses_to_compare_two_different_purposes() -> None:
    with pytest.raises(ContractError, match="one consumer and purpose"):
        reevaluate_consumer(use(), use(purpose=WitnessPurpose.DISCLOSE))


# --------------------------------------------------------------------------- #
# structural guards
# --------------------------------------------------------------------------- #


def test_a_justification_without_premises_is_refused() -> None:
    with pytest.raises(ContractError, match="at least 1"):
        JustificationSet(conclusion="pred-a@1#abc", premises=(), rule_version="rule-v1")


def test_a_justification_needs_a_rule_version() -> None:
    with pytest.raises(ContractError, match="rule_version"):
        JustificationSet(conclusion="pred-a@1#abc", premises=("pred-b@1#abc",))


def test_a_justification_may_not_carry_executable_content() -> None:
    entry = JustificationSet(
        conclusion="pred-a@1#abc",
        premises=("pred-b@1#abc",),
        rule_version="rule-v1",
        source_group="lambda x: True",
    )
    with pytest.raises(ContractError, match="executable"):
        SupportGraph([entry])


def test_the_support_graph_is_immutable() -> None:
    graph = SupportGraph([rule("pred-a@1#abc", "pred-b@1#abc")])
    with pytest.raises(ContractError, match="immutable"):
        graph._by_signature = {}  # type: ignore[misc]
    with pytest.raises(ContractError, match="immutable"):
        del graph._by_signature


def test_removing_one_group_yields_a_new_graph_and_leaves_the_original() -> None:
    first = rule("pred-k@1#abc", "pred-a@1#abc", version="rule-1")
    second = rule("pred-k@1#abc", "pred-b@1#abc", version="rule-2")
    graph = SupportGraph([first, second])
    pruned = graph.without_signature(first.signature)
    assert len(graph) == 2
    assert len(pruned) == 1
    assert pruned.groups_for(Atom("pred-k@1#abc"))[0].rule_version == "rule-2"


def test_a_justification_round_trips_through_json() -> None:
    entry = JustificationSet(
        conclusion="pred-k@1#abc",
        polarity=Polarity.NEGATIVE,
        premises=("pred-a@1#abc", evidence_ref("e1")),
        conditions=(RuleCondition(name="reviewed"),),
        rule_version="rule-1",
        receipt_ref=tref(TypedRefKind.REVIEW, "review-9"),
        source_group="lab-a",
    )
    restored = JustificationSet.from_json(entry.to_json())
    assert restored == entry
    assert restored.signature == entry.signature


def test_polarity_accepts_the_plus_and_minus_spellings() -> None:
    assert parse_polarity("+") is Polarity.POSITIVE
    assert parse_polarity("-") is Polarity.NEGATIVE
    assert parse_polarity(True) is Polarity.POSITIVE
    assert parse_polarity(False) is Polarity.NEGATIVE
    assert Atom("pred-a@1#abc", Polarity.POSITIVE).negated.polarity is Polarity.NEGATIVE


def test_a_premise_must_be_a_key_an_atom_or_an_evidence_ref() -> None:
    with pytest.raises(ContractError, match="proposition key"):
        JustificationSet(conclusion="pred-a@1#abc", premises=(17,), rule_version="rule-1")


def test_a_group_may_not_repeat_the_same_premise() -> None:
    with pytest.raises(ContractError, match="repeat a premise"):
        JustificationSet(
            conclusion="pred-a@1#abc",
            premises=("pred-b@1#abc", "pred-b@1#abc"),
            rule_version="rule-1",
        )


def test_each_signature_enters_the_closure_at_most_once() -> None:
    world = World({}, declared_only=frozenset({"a", "b"}))
    graph = SupportGraph(
        [
            rule(world.keys["b"], Atom(world.keys["a"]), version="rule-1"),
            rule(world.keys["b"], Atom(world.keys["a"]), version="rule-2"),
        ]
    )
    anchors = anchors_for(world, candidate(world.keys["a"], name="a"))
    result = grounded_closure(graph, anchors)
    assert len(result.witnesses_for(Atom(world.keys["b"]))) == 2
    assert result.node_count == 3
    assert len(result.admitted_signatures) == 3


def test_the_anchor_origin_is_carried_into_the_witness_path() -> None:
    world = World({}, declared_only=frozenset({"a"}))
    anchors = anchors_for(
        world, candidate(world.keys["a"], name="check", origin=AnchorOrigin.CHECK)
    )
    assert anchors[0].origin is AnchorOrigin.CHECK
    witness = grounded_closure(SupportGraph(), anchors).witnesses_for(Atom(world.keys["a"]))[0]
    assert witness.kind is WitnessKind.ANCHOR
    assert witness.source_group == "group-1"


def test_a_proposition_premise_records_the_atom_that_carried_it() -> None:
    world = World({}, declared_only=frozenset({"a", "b"}))
    graph = SupportGraph([rule(world.keys["b"], Atom(world.keys["a"]))])
    anchors = anchors_for(world, candidate(world.keys["a"], name="a"))
    witness = grounded_closure(graph, anchors).witnesses_for(Atom(world.keys["b"]))[0]
    assert witness.premises == (PropositionPremise(atom=Atom(world.keys["a"])),)
    assert witness.depth == 1


def test_the_selector_reports_its_own_scope_purpose_and_as_of() -> None:
    picker = selector(purpose=WitnessPurpose.PLAN)
    assert (picker.scope_id, picker.purpose, picker.as_of_ms) == (
        SCOPE,
        WitnessPurpose.PLAN,
        AS_OF,
    )


def test_the_selector_only_accepts_anchor_candidates() -> None:
    with pytest.raises(ContractError, match="AnchorCandidate"):
        selector().select(["obs-1"])  # type: ignore[list-item]


def test_grounded_closure_requires_a_support_graph() -> None:
    with pytest.raises(ContractError, match="SupportGraph"):
        grounded_closure(object(), ())  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# independent review fixes (2026-09-16)
# --------------------------------------------------------------------------- #


def _non_defeasible_graph(world: World) -> SupportGraph:
    return SupportGraph(
        [
            rule(
                world.keys["b"],
                Atom(world.keys["a"]),
                conditions=(RuleCondition(name="signed-off", defeasible=False),),
            )
        ]
    )


def test_a_non_defeasible_condition_met_by_assertion_is_still_assumed_support() -> None:
    """``defeasible=False`` must not be a way to launder an assumption."""

    world = World({}, declared_only=frozenset({"a", "b"}))
    anchors = anchors_for(world, candidate(world.keys["a"], name="a"))
    result = grounded_closure(
        _non_defeasible_graph(world), anchors, assumptions={"signed-off": True}
    )
    assert result.truth_for(world.keys["b"]) is TruthValue.TRUE
    assert result.rests_on_assumption(world.keys["b"])


def test_a_high_risk_gate_refuses_support_resting_on_any_assertion() -> None:
    world = World({}, declared_only=frozenset({"a", "b"}))
    anchors = anchors_for(world, candidate(world.keys["a"], name="a"))
    result = grounded_closure(
        _non_defeasible_graph(world), anchors, assumptions={"signed-off": True}
    )
    assert result.may_release(world.keys["b"], high_risk=False)
    assert not result.may_release(world.keys["b"], high_risk=True)
    assert result.release_block_reason(world.keys["b"], high_risk=True) == UNVERIFIED_ASSUMPTION


def test_the_witness_names_every_assertion_it_leaned_on() -> None:
    world = World({}, declared_only=frozenset({"a", "b"}))
    anchors = anchors_for(world, candidate(world.keys["a"], name="a"))
    result = grounded_closure(
        _non_defeasible_graph(world), anchors, assumptions={"signed-off": True}
    )
    witness = result.witnesses_for(Atom(world.keys["b"]))[0]
    assert witness.assumptions == ("signed-off",)


def test_a_release_gate_must_say_which_kind_of_gate_it_is() -> None:
    world = World({}, declared_only=frozenset({"a"}))
    result = grounded_closure(SupportGraph(), ())
    with pytest.raises(TypeError):
        result.may_release(world.keys["a"])  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        result.release_block_reason(world.keys["a"])  # type: ignore[call-arg]


def test_a_selector_without_a_registry_binding_is_refused() -> None:
    with pytest.raises(TypeError):
        AnchorSelector(scope_id=SCOPE, purpose=WitnessPurpose.ACCEPT, as_of_ms=AS_OF)  # type: ignore[call-arg]
    with pytest.raises(ContractError, match="signatures is required"):
        AnchorSelector(
            scope_id=SCOPE,
            purpose=WitnessPurpose.ACCEPT,
            as_of_ms=AS_OF,
            signatures=None,  # type: ignore[arg-type]
        )


def test_an_unbound_selector_cannot_admit_a_denial_by_default() -> None:
    world = World({}, closed=frozenset({"paid"}))
    unbound = AnchorSelector(
        scope_id=SCOPE, purpose=WitnessPurpose.ACCEPT, as_of_ms=AS_OF, signatures={}
    )
    selection = unbound.select(
        [
            candidate(
                world.keys["paid"],
                name="registry",
                polarity=False,
                authoritative=True,
                observer_id="observer-1",
            )
        ]
    )
    assert selection.anchors == ()
    assert selection.reason_for("obs-registry") is AnchorRejection.UNREGISTERED_PREDICATE


def test_a_denial_without_a_named_observer_is_refused_in_a_closed_domain() -> None:
    world = World({}, closed=frozenset({"paid"}))
    selection = selector(world).select(
        [candidate(world.keys["paid"], name="anon", polarity=False, authoritative=True)]
    )
    assert selection.reason_for("obs-anon") is AnchorRejection.OBSERVER_NOT_AUTHORITATIVE


def _conjunction_world() -> tuple[World, SupportGraph]:
    world = World({}, declared_only=frozenset({"a", "b", "c"}))
    graph = SupportGraph([rule(world.keys["c"], Atom(world.keys["a"]), Atom(world.keys["b"]))])
    return world, graph


def test_one_conjunctive_path_is_one_reason_even_across_two_sources() -> None:
    """AER §9.1 asks for two independent sufficient supports, not two names."""

    world, graph = _conjunction_world()
    anchors = anchors_for(
        world,
        candidate(world.keys["a"], name="lab", group="lab-a"),
        candidate(world.keys["b"], name="field", group="field-b"),
    )
    result = grounded_closure(graph, anchors)
    assert result.truth_for(world.keys["c"]) is TruthValue.TRUE
    assert result.independent_source_groups(world.keys["c"]) == frozenset({"lab-a", "field-b"})
    assert len(result.support_paths(world.keys["c"])) == 1
    assert not result.satisfies_independence(world.keys["c"], required=2)
    assert result.satisfies_independence(world.keys["c"], required=1)


def test_two_source_disjoint_paths_do_satisfy_the_policy() -> None:
    world = World({}, declared_only=frozenset({"a", "b", "c"}))
    graph = SupportGraph(
        [
            rule(world.keys["c"], Atom(world.keys["a"]), version="rule-c-from-a"),
            rule(world.keys["c"], Atom(world.keys["b"]), version="rule-c-from-b"),
        ]
    )
    anchors = anchors_for(
        world,
        candidate(world.keys["a"], name="lab", group="lab-a"),
        candidate(world.keys["b"], name="field", group="field-b"),
    )
    result = grounded_closure(graph, anchors)
    assert len(result.support_paths(world.keys["c"])) == 2
    assert result.satisfies_independence(world.keys["c"], required=2)
    assert not result.satisfies_independence(world.keys["c"], required=3)


def test_two_paths_that_share_a_source_are_not_independent() -> None:
    world = World({}, declared_only=frozenset({"a", "b", "c"}))
    graph = SupportGraph(
        [
            rule(world.keys["c"], Atom(world.keys["a"]), version="rule-c-from-a"),
            rule(world.keys["c"], Atom(world.keys["b"]), version="rule-c-from-b"),
        ]
    )
    anchors = anchors_for(
        world,
        candidate(world.keys["a"], name="first", group="wire-service"),
        candidate(world.keys["b"], name="second", group="wire-service"),
    )
    result = grounded_closure(graph, anchors)
    assert len(result.support_paths(world.keys["c"])) == 2
    assert not result.satisfies_independence(world.keys["c"], required=2)


def test_an_anchor_cannot_be_assembled_by_hand() -> None:
    world = World({}, declared_only=frozenset({"a"}))
    with pytest.raises(ContractError, match="AnchorSelector.select only"):
        Anchor(
            atom=Atom(world.keys["a"]),
            anchor_id="obs-forged",
            source_group="group-1",
            origin=AnchorOrigin.OBSERVATION,
        )


def test_a_forged_anchor_cannot_be_smuggled_past_the_closure() -> None:
    world = World({}, declared_only=frozenset({"a"}))
    real = anchors_for(world, candidate(world.keys["a"], name="a"))[0]

    class Forged:
        atom = Atom(world.keys["a"])
        anchor_id = "obs-forged"
        source_group = "group-1"
        signature = "anchor:obs-forged"

    with pytest.raises(ContractError, match="AnchorSelector output"):
        grounded_closure(SupportGraph(), [real, Forged()])  # type: ignore[list-item]
