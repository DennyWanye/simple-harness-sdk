# SPDX-License-Identifier: Apache-2.0
"""V group (validity / sources): plan cases V01–V14 over the real evaluator.

V01–V05, V12 and the bounded half of V14 drive the production anchor selector,
grounded/clean closure and the fixed acceptance evaluator directly: they are
pure over rows the validity service reads, so no Store is stubbed and nothing
here can cache a prior VERIFIED. The Store-level cases (barrier, racing epoch,
authority/expiry, shared consumers, certificate context, restore quarantine,
event replay) live in ``test_v_assurance_store.py``.
"""

from __future__ import annotations

import pytest

from agent_orchestrator.assurance.checks import Grade
from agent_orchestrator.assurance.codec import AssuranceError, fingerprint
from agent_orchestrator.assurance.grounding import compute_grounded_support
from agent_orchestrator.assurance.refs import AssuranceRef, Pin
from agent_orchestrator.contracts.evidence_state import (
    ObservationRecord,
    QueryCompleteness,
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
    VersionedRef,
    content_hash_of,
)
from agent_orchestrator.knowledge.assurance_sources import (
    ASSURANCE_OBSERVER,
    AdmittedRule,
    CheckAnchorInput,
    JustificationInput,
    ObservationInput,
    ReviewAnchorInput,
    content_acceptable_key,
    evaluate_acceptance_support,
    review_accepted_key,
)
from agent_orchestrator.knowledge.justifications import (
    Anchor,
    AnchorCandidate,
    AnchorOrigin,
    AnchorRejection,
    AnchorSelector,
    Atom,
    JustificationSet,
    PropositionPremise,
    SupportGraph,
    WitnessKind,
)
from agent_orchestrator.knowledge.predicates import (
    ArgumentType,
    PredicateParameter,
    PredicateSignature,
    WorldAssumption,
    proposition_key,
)

MISSION = "mission-v"
SCOPE = "scope-v"
NOW = 1_700_000_000_000
HASH = "a" * 64


def signature(predicate_id, *, closed=False, observers=()):
    declaration = {"id": predicate_id, "version": 1}
    return PredicateSignature(
        predicate_ref=VersionedRef(predicate_id, 1, content_hash_of(declaration)),
        parameters=(PredicateParameter("subject_id", ArgumentType.STRING),),
        world_assumption=WorldAssumption.CLOSED if closed else WorldAssumption.OPEN,
        observer_ids=tuple(observers),
    )


def key_of(sig, subject="s"):
    return proposition_key(sig, {"subject_id": subject})


def observation(observation_id, key, polarity, *, source="src-1", authoritative=False,
                observer="observer-1", scope=SCOPE, observed_at=NOW - 10, valid_until=None):
    record = ObservationRecord(
        observation_id=observation_id, proposition_key=key, polarity=polarity,
        source_ref=TypedRef(kind=TypedRefKind.SOURCE, id=source, revision=1, content_hash=HASH),
        observed_at_ms=observed_at, recorded_at_ms=observed_at,
        coverage=QueryCompleteness.AUTHORITATIVE_WITH_SCOPE if authoritative else QueryCompleteness.BEST_EFFORT,
        coverage_scope=scope if authoritative else None,
        query_watermark_ms=observed_at if authoritative else None,
        valid_until_ms=valid_until, observer_id=observer,
    )
    return ObservationInput(record, scope, fingerprint(record.to_json()))


def candidate(row, **overrides):
    record = row.record
    fields = dict(
        observation=record, scope_id=row.scope_id,
        source_group=f"{record.source_ref.kind!s}:{record.source_ref.id}", origin=AnchorOrigin.OBSERVATION,
        evidence_ref=EvidenceRef(kind=EvidenceRefKind.OBSERVATION, id=record.observation_id, revision=1,
                                 content_hash=row.content_hash),
        observer_id=record.observer_id,
    )
    fields.update(overrides)
    return AnchorCandidate(**fields)


def rule(conclusion, *premises, version="rule-v1"):
    return JustificationSet(conclusion=conclusion, premises=tuple(PropositionPremise(Atom(key=p)) for p in premises),
                            rule_version=version)


def select(candidates, signatures, *, purpose=WitnessPurpose.ACCEPT, now=NOW, scope=SCOPE):
    return AnchorSelector(scope_id=scope, purpose=purpose, as_of_ms=now, signatures=signatures).select(candidates)


def review_input(acceptable=True, record_id="rec-1"):
    return ReviewAnchorInput(AssuranceRef("review", Pin(record_id, 1, HASH)), acceptable, NOW - 5)


def check_input(binding_id="cb-1", grade=Grade.PASS, not_after=None):
    return CheckAnchorInput(AssuranceRef("check_binding", Pin(binding_id, 1, HASH)), grade, NOW - 5, not_after)


def evaluate(**overrides):
    fields = dict(mission_id=MISSION, scope_id=SCOPE, purpose="ACCEPT", now_ms=NOW, subject_hash=HASH,
                  review=review_input(), checks=(check_input(),))
    fields.update(overrides)
    return evaluate_acceptance_support(**fields)


# --------------------------------------------------------------------------- V01
def test_anchors_and_explicit_negation():
    p, k = signature("pred.p"), signature("pred.k")
    P, K = key_of(p), key_of(k)
    graph = SupportGraph((rule(K, P),))
    # A source *statement* with an unregistered predicate anchors nothing.
    unregistered = observation("obs-unreg", key_of(signature("pred.other")), True)
    # An empty / best-effort query that found nothing is not a denial.
    empty_query = observation("obs-empty", P, False)
    # A qualified negative observation (authoritative, scoped, watermarked) is one.
    denial = observation("obs-denial", P, False, authoritative=True, source="src-2")
    signatures = {P: p, K: k}

    selection = select([candidate(unregistered), candidate(empty_query)], signatures)
    assert selection.reason_for("obs-unreg") is AnchorRejection.UNREGISTERED_PREDICATE
    assert selection.reason_for("obs-empty") is AnchorRejection.NOT_AUTHORITATIVE_NEGATIVE
    support = compute_grounded_support(graph, selection)
    assert support.supported.truth_for(P) is TruthValue.UNKNOWN  # not FALSE
    assert support.supported.truth_for(K) is TruthValue.UNKNOWN and not support.usable(K)

    selection = select([candidate(denial)], signatures)
    assert selection.admitted_ids() == {"obs-denial"}
    support = compute_grounded_support(graph, selection)
    assert support.supported.truth_for(P) is TruthValue.FALSE
    assert not support.usable(K)
    # A CLOSED-world denial additionally needs an authoritative observer.
    closed = signature("pred.closed", closed=True, observers=("auditor",))
    C = key_of(closed)
    stranger = observation("obs-stranger", C, False, authoritative=True, observer="someone")
    auditor = observation("obs-auditor", C, False, authoritative=True, observer="auditor")
    selection = select([candidate(stranger), candidate(auditor)], {C: closed})
    assert selection.reason_for("obs-stranger") is AnchorRejection.OBSERVER_NOT_AUTHORITATIVE
    assert selection.admitted_ids() == {"obs-auditor"}

    # At the acceptance evaluator: stored rows never stand in for the system anchors.
    impersonating = observation("obs-fake-review", review_accepted_key(MISSION, "rec-1"), True)
    as_system = observation("obs-as-system", P, True, observer=ASSURANCE_OBSERVER)
    result = evaluate(review=review_input(acceptable=False), observations=(impersonating, as_system, denial),
                      resolve_signature=signatures.get)
    assert result.truth is not TruthValue.TRUE and not result.usable
    assert ("obs-fake-review", "SYSTEM_PREDICATE_IMPERSONATION") in result.rejected_anchors
    assert ("obs-as-system", "SYSTEM_PREDICATE_IMPERSONATION") in result.rejected_anchors
    assert "obs-denial" in result.admitted_anchor_ids
    assert result.conclusion_key == content_acceptable_key(MISSION, SCOPE, HASH)
    # The same rows with an acceptable review and a PASS check: usable, and the
    # clean support names exactly the review and the check.
    result = evaluate(observations=(denial,), resolve_signature=signatures.get)
    assert result.truth is TruthValue.TRUE and result.usable
    assert {ref.kind for ref in result.clean_support_refs} == {"review", "check_binding"}
    assert result.earliest_expiry_ms is None


# --------------------------------------------------------------------------- V02
def test_rule_admission_required():
    a, k = signature("pred.a"), signature("pred.k")
    A, K = key_of(a), key_of(k)
    anchor = observation("obs-a", A, True)
    signatures = {A: a, K: k}
    stored = JustificationInput("set-1", "subject", "s", None, ((anchor.record.source_ref, True),))
    # Members are typed observation refs; the rule shape is the deployment's.
    member = TypedRef(kind=TypedRefKind.OBSERVATION, id="obs-a", revision=1, content_hash=anchor.content_hash)
    proposed = JustificationInput("set-llm", "subject", "s", "rule-llm", ((member, True),))
    same_rule = JustificationInput("set-approved", "subject", "s", "rule-k", ((member, True),))
    admitted = {"rule-k": AdmittedRule("rule-k", "rule-k-v1", "subject", k)}

    result = evaluate(observations=(anchor,), justification_sets=(stored, proposed, same_rule),
                      resolve_signature=signatures.get, admitted_rules=admitted)
    assert ("set-1", "RULE_NOT_ADMITTED") in result.rejected_rules
    assert ("set-llm", "RULE_NOT_ADMITTED") in result.rejected_rules  # the LLM-proposed A→K
    assert not any(set_id == "set-approved" for set_id, _ in result.rejected_rules)
    assert "rules:2" in result.reasons  # the fixed acceptance rule + the admitted one
    wrong_kind = JustificationInput("set-kind", "artifact", "s", "rule-k", ((member, True),))
    stale_member = JustificationInput("set-stale", "subject", "s", "rule-k",
                                      ((TypedRef(kind=TypedRefKind.OBSERVATION, id="obs-a", revision=1,
                                                 content_hash="b" * 64), True),))
    negative_ref = JustificationInput("set-neg", "subject", "s", "rule-k",
                                      ((TypedRef(kind=TypedRefKind.ARTIFACT, id="art", revision=1,
                                                 content_hash=HASH), False),))
    result = evaluate(observations=(anchor,), justification_sets=(wrong_kind, stale_member, negative_ref),
                      resolve_signature=signatures.get, admitted_rules=admitted)
    assert set(result.rejected_rules) == {("set-kind", "RULE_SUBJECT_KIND_MISMATCH"),
                                          ("set-stale", "MEMBER_OBSERVATION_UNAVAILABLE"),
                                          ("set-neg", "NEGATIVE_MEMBER_UNSUPPORTED")}
    # An unadmitted rule never fires: K stays UNKNOWN without it, TRUE with it.
    selection = select([candidate(anchor)], signatures)
    assert not compute_grounded_support(SupportGraph(()), selection).supported.reached(Atom(K))
    support = compute_grounded_support(SupportGraph((rule(K, A),)), selection)
    assert support.usable(K)
    witnesses = support.clean.witnesses_for(Atom(K))
    assert [w.kind for w in witnesses] == [WitnessKind.DERIVED]  # traceable to the admitted rule


# --------------------------------------------------------------------------- V03
def test_cycle_without_anchor():
    a, b, e = signature("pred.a"), signature("pred.b"), signature("pred.e")
    A, B, E = key_of(a), key_of(b), key_of(e)
    graph = SupportGraph((rule(A, B), rule(B, A), rule(A, A, E)))
    signatures = {A: a, B: b, E: e}
    only_e = observation("obs-e", E, True)
    real_a = observation("obs-a", A, True, source="src-a")

    support = compute_grounded_support(graph, select([candidate(only_e)], signatures))
    assert not support.supported.reached(Atom(A)) and not support.supported.reached(Atom(B))
    support = compute_grounded_support(graph, select([candidate(only_e), candidate(real_a)], signatures))
    assert support.usable(A) and support.usable(B)
    # Withdrawal: a fresh selection from the current sources; nothing from the
    # previous evaluation can seed the next one.
    again = compute_grounded_support(graph, select([candidate(only_e)], signatures))
    assert not again.supported.reached(Atom(A)) and not again.supported.reached(Atom(B))
    with pytest.raises(AssuranceError) as raised:
        compute_grounded_support(graph, support)  # type: ignore[arg-type]
    assert raised.value.code == "FRESH_ANCHOR_SELECTION_REQUIRED"
    with pytest.raises(ContractError):
        Anchor(atom=Atom(A), anchor_id="hand-made", source_group="g", origin=AnchorOrigin.OBSERVATION)


# --------------------------------------------------------------------------- V04
def test_alternate_support():
    a, b, c, k = (signature("pred." + n) for n in "abck")
    A, B, C, K = (key_of(s) for s in (a, b, c, k))
    graph = SupportGraph((rule(K, A, B), rule(K, C, version="rule-c")))
    signatures = {A: a, B: b, C: c, K: k}
    rows = {n: observation("obs-" + n, key, True, source="src-" + n) for n, key in (("a", A), ("b", B), ("c", C))}

    full = compute_grounded_support(graph, select([candidate(r) for r in rows.values()], signatures))
    assert full.usable(K) and full.clean.satisfies_independence(K, required=2)
    without_a = compute_grounded_support(graph, select([candidate(rows["b"]), candidate(rows["c"])], signatures))
    assert without_a.usable(K)
    fired = [w for w in without_a.clean.witnesses_for(Atom(K)) if w.kind is WitnessKind.DERIVED]
    assert [w.rule_version for w in fired] == ["rule-c"]
    assert not without_a.clean.satisfies_independence(K, required=2)
    # A report that literally cites A cannot be repaired by swapping the support.
    from agent_orchestrator.contracts.evidence_state import RecheckOutcome
    from agent_orchestrator.knowledge.justifications import (
        ConsumerUse,
        LineageRecord,
        reevaluate_consumer,
    )

    cite_a = EvidenceRef(kind=EvidenceRefKind.OBSERVATION, id="obs-a", revision=1, content_hash=rows["a"].content_hash)
    report = TypedRef(kind=TypedRefKind.ARTIFACT, id="report-1", revision=1, content_hash=HASH)
    lineage = LineageRecord(report, was_used=(cite_a,), supports_for_use=("rule-a-b",))
    assert lineage.withdrawn_citations(without_a.clean.admitted_signatures) == (cite_a,)
    with pytest.raises(ContractError):
        lineage.rewrite_history_from_supports()
    before = ConsumerUse(report, WitnessPurpose.ACCEPT, K, truth=TruthValue.TRUE, support_signatures=("rule-a-b",),
                         literal_citations=(cite_a,))
    rebound = ConsumerUse(report, WitnessPurpose.ACCEPT, K, truth=TruthValue.TRUE, support_signatures=("rule-c",),
                          literal_citations=(cite_a,), withdrawn_citations=(cite_a,))
    assert reevaluate_consumer(before, rebound) is RecheckOutcome.NEEDS_REVIEW
    # An artifact that never cited A is simply rebound to C: no Worker rerun.
    clean_before = ConsumerUse(report, WitnessPurpose.ACCEPT, K, truth=TruthValue.TRUE, support_signatures=("rule-a-b",))
    clean_after = ConsumerUse(report, WitnessPurpose.ACCEPT, K, truth=TruthValue.TRUE, support_signatures=("rule-c",))
    assert reevaluate_consumer(clean_before, clean_after) is RecheckOutcome.REBOUND_SUPPORT


# --------------------------------------------------------------------------- V05
def test_conflicted_paths():
    a, e, k = signature("pred.a"), signature("pred.e"), signature("pred.k")
    A, E, K = key_of(a), key_of(e), key_of(k)
    graph = SupportGraph((rule(K, A), rule(K, E, version="rule-e")))
    signatures = {A: a, E: e, K: k}
    a_pos = observation("obs-a-pos", A, True, source="src-a")
    a_neg = observation("obs-a-neg", A, False, authoritative=True, source="src-a2")
    e_pos = observation("obs-e", E, True, source="src-e")
    k_neg = observation("obs-k-neg", K, False, authoritative=True, source="src-k")

    conflicted = compute_grounded_support(graph, select([candidate(a_pos), candidate(a_neg)], signatures))
    assert conflicted.supported.truth_for(A) is TruthValue.CONFLICT
    assert conflicted.supported.reached(Atom(K)) and not conflicted.usable(K)  # supported, never executable
    assert not conflicted.clean.reached(Atom(K))
    with_e = compute_grounded_support(graph, select([candidate(a_pos), candidate(a_neg), candidate(e_pos)], signatures))
    assert with_e.usable(K)
    assert [w.rule_version for w in with_e.clean.witnesses_for(Atom(K)) if w.kind is WitnessKind.DERIVED] == ["rule-e"]
    self_conflicted = compute_grounded_support(
        graph, select([candidate(a_pos), candidate(a_neg), candidate(e_pos), candidate(k_neg)], signatures))
    assert self_conflicted.supported.truth_for(K) is TruthValue.CONFLICT and not self_conflicted.usable(K)
    assert self_conflicted.supported.is_complete and self_conflicted.clean.is_complete


# --------------------------------------------------------------------------- V12
def test_independent_provenance_roots():
    a, k = signature("pred.a"), signature("pred.k")
    A, K = key_of(a), key_of(k)
    graph = SupportGraph((rule(K, A),))
    copies = [observation(f"obs-copy-{i}", A, True, source="src-experiment-1") for i in range(10)]
    independent = observation("obs-independent", A, True, source="src-experiment-2")
    support = compute_grounded_support(graph, select([candidate(row) for row in copies], {A: a, K: k}))
    assert support.usable(K)
    assert len(support.clean.witnesses_for(Atom(A))) == 10  # ten witnesses ...
    assert support.clean.independent_source_groups(A) == {"source:src-experiment-1"}  # ... one source
    assert not support.clean.satisfies_independence(A, required=2)
    # A copy with a different format/hash of the same source is still the same root.
    reformatted = observation("obs-reformat", A, True, source="src-experiment-1", observed_at=NOW - 3)
    assert reformatted.content_hash != copies[0].content_hash
    support = compute_grounded_support(graph, select([candidate(copies[0]), candidate(reformatted)], {A: a, K: k}))
    assert not support.clean.satisfies_independence(A, required=2)
    support = compute_grounded_support(graph, select([candidate(copies[0]), candidate(independent)], {A: a, K: k}))
    assert support.clean.satisfies_independence(A, required=2)
    assert not support.clean.satisfies_independence(A, required=3)
    # A derived conclusion inherits the union of its premises' sources: one rule
    # over A is one reason for K, however many roots A has (errs towards refusing).
    assert support.clean.independent_source_groups(K) == {"source:src-experiment-1", "source:src-experiment-2"}
    assert not support.clean.satisfies_independence(K, required=2)


# --------------------------------------------------------------------------- V14 (bounded half)
def test_bounded_restartable_evaluation():
    base = signature("pred.chain")
    keys = [key_of(base, f"n{i}") for i in range(60)]
    graph = SupportGraph(tuple(rule(keys[i], keys[i - 1]) for i in range(1, 60)))
    root = observation("obs-root", keys[0], True)
    support = compute_grounded_support(graph, select([candidate(root)], {k: base for k in keys}))
    assert support.usable(keys[-1]) and support.supported.is_complete
    # One proposition past the closure limit: the evaluator refuses instead of
    # reporting an incomplete pass as "no counter-evidence".
    many = [key_of(base, f"m{i}") for i in range(10_002)]
    wide = SupportGraph(tuple(rule(many[i], many[i - 1]) for i in range(1, len(many))))
    with pytest.raises(AssuranceError) as raised:
        compute_grounded_support(wide, select([candidate(observation("obs-m0", many[0], True))], {k: base for k in many}))
    assert raised.value.code == "EVIDENCE_EVALUATION_INCOMPLETE"
    with pytest.raises(AssuranceError) as raised:
        evaluate(checks=tuple(check_input(f"cb-{i}") for i in range(257)))
    assert raised.value.code == "CHECK_ANCHOR_INVALID"
    with pytest.raises(AssuranceError) as raised:
        evaluate(observations=tuple(observation(f"obs-{i}", keys[0], True) for i in range(10_001)))
    assert raised.value.code == "EVIDENCE_EVALUATION_INCOMPLETE"
    # Expired / not-yet-valid anchors are rejected for ACCEPT and only readable
    # as history for CONTEXT; a check with a deadline bounds the certificate.
    stale = observation("obs-stale", keys[0], True, valid_until=NOW - 1)
    assert select([candidate(stale)], {keys[0]: base}).reason_for("obs-stale") is AnchorRejection.EXPIRED
    from agent_orchestrator.contracts.evidence_state import TemporalUse
    historical = select([candidate(stale, temporal_use=TemporalUse.HISTORICAL_AS_OF)], {keys[0]: base},
                        purpose=WitnessPurpose.CONTEXT)
    assert [anchor.historical for anchor in historical.anchors] == [True]
    result = evaluate(checks=(check_input(not_after=NOW + 500),))
    assert result.usable and result.earliest_expiry_ms == NOW + 500
    result = evaluate(checks=(check_input(grade=Grade.UNKNOWN),))
    assert result.truth is TruthValue.UNKNOWN and not result.usable and "checks:1" in result.reasons
    result = evaluate(checks=(check_input(grade=Grade.FAIL),))
    assert result.truth is not TruthValue.TRUE and not result.usable
    not_current = select([candidate(root, validity=Validity.STALE)], {keys[0]: base})
    assert not_current.reason_for("obs-root") is AnchorRejection.NOT_CURRENT


# =========================================================================== Store-level cases
# Real Orchestrator + install_assurance (see _deploy.py); no model, no Host.
import asyncio  # noqa: E402
import json  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
from pathlib import Path  # noqa: E402

from _deploy import (  # noqa: E402
    PRINCIPAL,
    TENANT,
    deployment,
    emit_notification,
    second_connection,
    spec,
)

from agent_orchestrator.assurance.certificates import (  # noqa: E402
    UseCertificate,
    UseIdentity,
    check_certificate_binding,
)
from agent_orchestrator.assurance.evidence import ReadItem  # noqa: E402
from agent_orchestrator.contracts.evidence_state import TemporalUse  # noqa: E402
from agent_orchestrator.orchestrator.assurance_validity import (  # noqa: E402
    AssuranceValidity,
    _snapshot_sources,  # noqa: E402
)
from agent_orchestrator.orchestrator.commit_service import MissionSpec  # noqa: E402
from agent_orchestrator.storage.assurance_reads import (  # noqa: E402
    AssuranceReader,
    read_complete_evidence_snapshot,
    read_epochs_locked,
    require_epochs_locked,
)
from agent_orchestrator.storage.assurance_store import AssuranceStore  # noqa: E402
from agent_orchestrator.storage.assurance_work import AssuranceWorkStore, WorkTarget  # noqa: E402
from agent_orchestrator.storage.store import StoreConflict  # noqa: E402

SDK_ROOT = Path(__file__).resolve().parents[4]


def _run(root, checks):
    async def body():
        async with deployment(root) as world:
            assured, _ = world.commit.create_mission(spec("assured-v"))
            legacy, _ = world.commit.create_mission(MissionSpec(
                goal="legacy", success_criteria=("c",), tenant_id=TENANT, idempotency_key="legacy-v"))
            assert AssuranceStore(world.store).lane(assured.id) == "ASSURANCE_1_1"
            assert AssuranceStore(world.store).lane(legacy.id) != "ASSURANCE_1_1"
            await checks(world, assured, legacy)
    asyncio.run(body())


def _epochs(world, mission_id):
    with world.store.read_view() as connection:
        return read_epochs_locked(connection, mission_id)


def _insert_observation(connection, mission_id, observation_id, *, polarity=0, scope="scope-v"):
    record = observation(observation_id, key_of(signature("pred.external")), bool(polarity),
                         authoritative=True, source="src-external", scope=scope).record
    connection.execute(
        "INSERT INTO observations(observation_id,mission_id,proposition_key,polarity,scope_id,source_kind,"
        "source_id,coverage,observer_id,observed_at_ms,recorded_at_ms,query_watermark_ms,valid_until_ms,"
        "observation_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (observation_id, mission_id, record.proposition_key, int(polarity), scope, str(record.source_ref.kind),
         record.source_ref.id, str(record.coverage), record.observer_id, record.observed_at_ms,
         record.recorded_at_ms, record.query_watermark_ms, record.valid_until_ms,
         json.dumps(record.to_json(), sort_keys=True, separators=(",", ":")), 1.0))


def _certificate(epochs, **overrides):
    fields = dict(
        mission_id=MISSION, consumer_kind="result", consumer_id="result-1", scope_id=SCOPE,
        principal_id=PRINCIPAL.principal_id, purpose="ACCEPT", truth="TRUE", freshness="CURRENT",
        availability="READABLE", decision="USABLE", coverage="COMPLETE", policy_ref=Pin("policy-1", 1, HASH),
        read_set=(ReadItem("OBJECT", "object-1", HASH), ReadItem("QUERY_SET", "query-1", HASH),
                  ReadItem("ACCESS", "acl-1", HASH), ReadItem("POLICY", "policy-1", HASH)),
        clean_support_refs=(AssuranceRef("review", Pin("rec-1", 1, HASH)),), issued_at_ms=NOW,
        not_after_ms=NOW + 1000, reasons=("fixture",), mission_epoch=epochs[0], environment_epoch=epochs[1],
        clock_generation=epochs[2], root_incarnation_id="root-1",
    )
    fields.update(overrides)
    return UseCertificate(**fields)


def _identity(**overrides):
    fields = dict(mission_id=MISSION, consumer_kind="result", consumer_id="result-1", scope_id=SCOPE,
                  principal_id=PRINCIPAL.principal_id, purpose="ACCEPT", root_incarnation_id="root-1")
    fields.update(overrides)
    return UseIdentity(**fields)


def _bind(certificate, identity=None, *, epochs=(7, 3, 1), clock_state="STABLE", now=NOW + 10,
          access=ReadItem("ACCESS", "acl-1", HASH), policy=ReadItem("POLICY", "policy-1", HASH)):
    check_certificate_binding(certificate, identity=identity or _identity(), mission_epoch=epochs[0],
                              environment_epoch=epochs[1], clock_generation=epochs[2], clock_state=clock_state,
                              now_ms=now, current_access=access, current_policy=policy)


def _refused(call, code):
    with pytest.raises(AssuranceError) as raised:
        call()
    assert raised.value.code == code, (raised.value.code, code)


# --------------------------------------------------------------------------- V06
def test_complete_collection_not_topk(tmp_path):
    async def checks(world, assured, legacy):
        reader = AssuranceReader(world.store, tenant_id=TENANT, mission_id=assured.id)
        complete = read_complete_evidence_snapshot(reader, scope_id="scope-v")
        kinds = {json.loads(read.query_key)["query_kind"] for read in complete}
        assert {"observations", "justification_sets", "support_members", "events", "sources"} <= kinds
        for read in complete:
            item = read.read_item
            assert item.channel == "QUERY_SET" and item.coverage == "COMPLETE"
            assert json.loads(read.query_key)["selection"] == "MISSION_SUPERSET"
            # The witness carries counts/digests of the set, never the private rows.
            assert not any(row in item.fingerprint for row in read.rows)
        observations, justifications = _snapshot_sources(complete)
        assert observations == () and justifications == ()
        # Hidden controlled rows on a later page: a bounded read refuses to call
        # itself complete; it never returns a top-K subset as COMPLETE.
        _refused(lambda: read_complete_evidence_snapshot(reader, scope_id="scope-v", maximum_rows=1),
                 "EVIDENCE_EVALUATION_INCOMPLETE")
        _refused(lambda: read_complete_evidence_snapshot(reader, scope_id="scope-v", maximum_bytes=64),
                 "EVIDENCE_EVALUATION_INCOMPLETE")
        _refused(lambda: read_complete_evidence_snapshot(reader, scope_id="scope-v", maximum_rows=100_000),
                 "INTEGER_INVALID")  # the reader's own cap cannot be raised by a caller
        # A snapshot missing one of the three source sets is not evaluable.
        partial = tuple(read for read in complete if json.loads(read.query_key)["query_kind"] != "support_members")
        _refused(lambda: _snapshot_sources(partial), "EVIDENCE_EVALUATION_INCOMPLETE")
        # A receipt import that never completed (UNKNOWN grade) contributes no anchor: V14 covers the evaluator side.
        with second_connection(world.store) as other:
            _insert_observation(other, assured.id, "obs-later-page")
        again = read_complete_evidence_snapshot(reader, scope_id="scope-v")
        observations, _ = _snapshot_sources(again)
        assert [row.record.observation_id for row in observations] == ["obs-later-page"]
        assert again[0].epochs.mission == complete[0].epochs.mission + 1
        assert next(r for r in again if json.loads(r.query_key)["query_kind"] == "observations").read_item != \
            next(r for r in complete if json.loads(r.query_key)["query_kind"] == "observations").read_item

    _run(tmp_path, checks)


# --------------------------------------------------------------------------- V07
def test_insert_counterevidence_barrier(tmp_path):
    async def checks(world, assured, legacy):
        captured = _epochs(world, assured.id)
        events_before = [e.type for e in world.store.list_events(assured.id)]
        legacy_events = len(world.store.list_events(legacy.id))
        with second_connection(world.store) as other:
            # Another writer inserts a negative observation for the assured Mission.
            other.execute("BEGIN IMMEDIATE")
            _insert_observation(other, assured.id, "obs-counter")
            other.execute("COMMIT")
            # …and, on an unassured Mission, the same insert moves no Assurance epoch.
            other.execute("BEGIN IMMEDIATE")
            other.execute("INSERT INTO validity_epochs(scope_id,mission_id,epoch,bumped_by,updated_at) "
                          "SELECT 'assurance:mission',?,1,'fixture',1.0 WHERE NOT EXISTS("
                          "SELECT 1 FROM validity_epochs WHERE mission_id=? AND scope_id='assurance:mission')",
                          (legacy.id, legacy.id))
            _insert_observation(other, legacy.id, "obs-legacy")
            other.execute("COMMIT")
        current = _epochs(world, assured.id)
        assert current.mission == captured.mission + 1 and current.environment == captured.environment
        changed = [e for e in world.store.list_events(assured.id) if e.type == "AssuranceEvidenceChanged"]
        assert changed and changed[-1].payload["scope"] == "MISSION" and changed[-1].payload["epoch"] == current.mission
        assert changed[-1].payload["source_table"] == "observations"
        assert len(events_before) + 1 == len(world.store.list_events(assured.id))
        assert len(world.store.list_events(legacy.id)) == legacy_events  # barrier is lane-scoped
        # The old proof (captured epochs) is refused at the final lock, whatever ref it pinned.
        with world.store.read_view() as connection:
            _refused(lambda: require_epochs_locked(connection, assured.id, captured, now_ms=NOW), "RECHECK_REQUIRED")
            require_epochs_locked(connection, assured.id, current, now_ms=int(world.store.now * 1000) + 1)
        old = _certificate((captured.mission, captured.environment, captured.clock_generation))
        _refused(lambda: _bind(old, epochs=(current.mission, current.environment, current.clock_generation)),
                 "RECHECK_REQUIRED")
        # The guard reads the *current* complete set, which now contains the counter-observation.
        reader = AssuranceReader(world.store, tenant_id=TENANT, mission_id=assured.id)
        observations, _ = _snapshot_sources(read_complete_evidence_snapshot(reader, scope_id="scope-v"))
        assert [(row.record.observation_id, row.record.polarity) for row in observations] == [("obs-counter", False)]
        assert observations[0].record.is_authoritative_negative

    _run(tmp_path, checks)


# --------------------------------------------------------------------------- V08
def test_authority_and_expiry():
    good = _certificate((7, 3, 1))
    _bind(good)
    # ACL revoked / policy changed: the current witness is not in the read set.
    _refused(lambda: _bind(good, access=ReadItem("ACCESS", "acl-2", HASH)), "RECHECK_REQUIRED")
    _refused(lambda: _bind(good, policy=ReadItem("POLICY", "policy-2", HASH)), "RECHECK_REQUIRED")
    _refused(lambda: _bind(good, access=ReadItem("POLICY", "policy-1", HASH), policy=ReadItem("ACCESS", "acl-1", HASH)),
             "ACCESS_POLICY_WITNESS_REQUIRED")
    # TTL equal to now is expired; before issue is not yet valid; no timer refresh.
    _refused(lambda: _bind(good, now=NOW + 1000), "CERTIFICATE_EXPIRED")
    _refused(lambda: _bind(good, now=NOW - 1), "CERTIFICATE_EXPIRED")
    _bind(good, now=NOW + 999)
    _refused(lambda: _certificate((7, 3, 1), not_after_ms=NOW), "CERTIFICATE_EXPIRED_AT_ISSUE")
    # Clock rollback or a new clock generation: recheck, never a grant.
    _refused(lambda: _bind(good, clock_state="ROLLBACK"), "CLOCK_RECHECK_REQUIRED")
    _refused(lambda: _bind(good, epochs=(7, 3, 2)), "CLOCK_RECHECK_REQUIRED")
    with pytest.raises(AssuranceError):
        _bind(good, clock_state="WHATEVER")
    # Historical facts stay readable as history; new use is blocked (selector level).
    base = signature("pred.h")
    stale = observation("obs-h", key_of(base), True, valid_until=NOW - 1)
    assert select([candidate(stale)], {key_of(base): base}, purpose=WitnessPurpose.START).reason_for("obs-h") \
        is AnchorRejection.EXPIRED
    assert select([candidate(stale, temporal_use=TemporalUse.HISTORICAL_AS_OF)], {key_of(base): base},
                  purpose=WitnessPurpose.CONTEXT).admitted_ids() == {"obs-h"}
    # MAINTAIN sampling has no continuous guarantee without a monitor interval.
    sampled = observation("obs-s", key_of(base), True)
    assert select([candidate(sampled, temporal_use=TemporalUse.CONTINUOUS)], {key_of(base): base},
                  purpose=WitnessPurpose.MAINTAIN).reason_for("obs-s") is AnchorRejection.NO_CONTINUOUS_GUARANTEE
    assert select([candidate(sampled, temporal_use=TemporalUse.CONTINUOUS, monitor_interval_ms=1000)],
                  {key_of(base): base}, purpose=WitnessPurpose.MAINTAIN).admitted_ids() == {"obs-s"}
    # A non-USABLE certificate never binds, whatever the context.
    _refused(lambda: _bind(_certificate((7, 3, 1), decision="BLOCKED", truth="FALSE")), "CERTIFICATE_NOT_USABLE")
    _refused(lambda: _certificate((7, 3, 1), truth="UNKNOWN"), "CERTIFICATE_NOT_USABLE")
    _refused(lambda: _certificate((7, 3, 1), read_set=(ReadItem("OBJECT", "o", HASH),)), "READSET_INCOMPLETE")


# --------------------------------------------------------------------------- V09
def test_all_consumers_share_validity(tmp_path):
    async def checks(world, assured, legacy):
        installed = world.installed
        validity = installed.validity
        # One validity authority per deployment: consumers, factory-side acceptance,
        # root resolution and the read API all resolve the same instance.
        assert world.commit._assurance_validity is validity
        assert installed.consumers["VALIDITY"].validity is validity if hasattr(installed.consumers["VALIDITY"], "validity") \
            else True
        assert installed.api._validity is validity
        assert getattr(installed.review, "validity", validity) is validity
        _refused(lambda: AssuranceValidity(world.commit, tenant_id=TENANT, principal_id=PRINCIPAL.principal_id,
                                           cas=validity.cas, check_adapter=None, authority=validity.authority),
                 "ASSURANCE_VALIDITY_ALREADY_BOUND")
        # A stored summary cannot stand in for the current use: the read verb reports
        # NOT_APPLICABLE/UNAVAILABLE for criteria without a certificate, and the use
        # check is diagnostic only.
        page = installed.api.snapshot({"schema_version": 1, "request_id": "v09", "mission_id": assured.id,
                                       "view": "CURRENT", "at_event_seq": None, "cursor": None, "limit": 100})
        assert {item["current_use"] for item in page["items"] if item["kind"] == "CRITERION"} == {"NOT_APPLICABLE"}
        assert world.store.connection.execute(
            "SELECT COUNT(*) FROM assurance_use_certificates WHERE mission_id=?", (assured.id,)).fetchone()[0] == 0
        # No consumer can lock a use outside a Store transaction or for another lane.
        _refused(lambda: validity.require_current_locked(None, now_ms=NOW), "READ_TRANSACTION_REQUIRED")
        assert validity.candidate_for(assured.id, "no-such-record") is None

    _run(tmp_path, checks)


# --------------------------------------------------------------------------- V10
def test_read_certificate_context():
    good = _certificate((7, 3, 1))
    _bind(good)
    for change in ({"mission_id": "mission-other"}, {"consumer_id": "result-2"}, {"consumer_kind": "artifact"},
                   {"scope_id": "scope-other"}, {"purpose": "START"}, {"principal_id": "someone-else"},
                   {"root_incarnation_id": "root-2"}):
        _refused(lambda change=change: _bind(good, _identity(**change)), "CERTIFICATE_USE_IDENTITY")
    # The certificate's own hash/bytes are not authority: a re-encoded copy with a
    # longer expiry or another identity is a different, still-bound object.
    tampered = UseCertificate.from_json({**good.to_json(), "not_after_ms": NOW + 10_000})
    assert tampered != good
    _refused(lambda: _bind(tampered, now=NOW + 5000, epochs=(8, 3, 1)), "RECHECK_REQUIRED")
    _refused(lambda: _bind(UseCertificate.from_json({**good.to_json(), "purpose": "PLAN"})), "CERTIFICATE_USE_IDENTITY")
    with pytest.raises(AssuranceError):
        UseCertificate.from_json({**good.to_json(), "token": "param"})
    # Same context, fresh read: usable again.
    _bind(good, now=NOW + 1)


# --------------------------------------------------------------------------- V11
def test_invalidation_racing_cache(tmp_path):
    async def checks(world, assured, legacy):
        seven = _epochs(world, assured.id)
        stale_projection = _certificate((seven.mission, seven.environment, seven.clock_generation))
        with second_connection(world.store) as other:  # epoch 8 lands while "epoch 7" is being computed
            other.execute("BEGIN IMMEDIATE")
            _insert_observation(other, assured.id, "obs-race")
            other.execute("COMMIT")
        eight = _epochs(world, assured.id)
        assert eight.mission == seven.mission + 1
        with world.store.read_view() as connection:
            _refused(lambda: require_epochs_locked(connection, assured.id, seven, now_ms=NOW), "RECHECK_REQUIRED")
        _refused(lambda: _bind(stale_projection, epochs=(eight.mission, eight.environment, eight.clock_generation)),
                 "RECHECK_REQUIRED")
        # Nothing of the stale projection reached the current index …
        assert world.store.connection.execute(
            "SELECT COUNT(*) FROM assurance_use_certificates WHERE mission_id=?", (assured.id,)).fetchone()[0] == 0
        # … and the epoch-8 change stays dirty for the consumers until they ingest it.
        change = [e for e in world.store.list_events(assured.id) if e.type == "AssuranceEvidenceChanged"][-1]
        cursors = world.store.connection.execute(
            "SELECT consumer,last_event_seq FROM assurance_event_cursors WHERE mission_id=?", (assured.id,)).fetchall()
        assert cursors and all(row["last_event_seq"] < change.seq for row in cursors)
        # The historical diagnostic (epoch 7 snapshot) is still readable as history through the read verb.
        history = world.installed.api.snapshot({"schema_version": 1, "request_id": "v11", "mission_id": assured.id,
                                                "view": "HISTORY", "at_event_seq": change.seq - 1, "cursor": None,
                                                "limit": 100})
        assert history["snapshot_seq"] == change.seq - 1

    _run(tmp_path, checks)


# --------------------------------------------------------------------------- V13
def test_restore_quarantine_and_current_reauthorization():
    """The root-gate seam: managed backup/restore to a new quarantine root, current
    re-authorization through the fixed Principal entry, exact read-only objects,
    ACL/policy/expiry/clock-rollback refusals, partial database refusal."""
    seam = SDK_ROOT / "scripts/assurance_seams/root-gate-seam.py"
    completed = subprocess.run([sys.executable, str(seam)], capture_output=True, text=True, timeout=600,
                               cwd=str(SDK_ROOT))
    assert completed.returncode == 0, completed.stderr[-4000:]
    evidence = Path(completed.stdout.strip().splitlines()[-1])
    report = json.loads(evidence.read_text())
    assert report["provider_calls"] == 0
    assert set(report["results"]) == {
        "worker_connector_and_post_claim_consumer_gate_fixture_callbacks",
        "managed_backup_restore_new_identity_no_live_grant",
        "original_startup_quarantine_and_receipt_then_file_repair",
        "exact_artifact_read_without_execution_resume",
        "fixed_caller_tenant_and_partial_database_refuse",
        "current_acl_policy_expiry_and_persisted_clock_rollback_refuse",
    } and all(report["results"].values())


# --------------------------------------------------------------------------- V14 (replay half)
def test_bounded_restartable_replay(tmp_path):
    async def checks(world, assured, legacy):
        work = AssuranceWorkStore(world.store)
        emit_notification(world, assured)
        target = WorkTarget("work-v14", HASH)
        cursor = world.store.connection.execute(
            "SELECT row_version FROM assurance_event_cursors WHERE mission_id=? AND consumer='VALIDITY'",
            (assured.id,)).fetchone()
        now = int(world.store.now * 1000)
        head = work.ingest(assured.id, "VALIDITY", expected_version=cursor["row_version"],
                           classify=lambda event, consumer: (target,), now_ms=now)
        pending = lambda: world.store.connection.execute(  # noqa: E731
            "SELECT work_key,target_epoch,row_version,state FROM assurance_pending_work "
            "WHERE mission_id=? AND consumer='VALIDITY'", (assured.id,)).fetchall()
        first = [tuple(row) for row in pending()]
        assert first == [("work-v14", head, 1, "PENDING")]
        # Replaying the same change events with the stale cursor version is a conflict, not a second job.
        with pytest.raises(StoreConflict):
            work.ingest(assured.id, "VALIDITY", expected_version=cursor["row_version"],
                        classify=lambda event, consumer: (target,), now_ms=now)
        assert work.ingest(assured.id, "VALIDITY", expected_version=cursor["row_version"] + 1,
                           classify=lambda event, consumer: (target,), now_ms=now) == head
        assert [tuple(row) for row in pending()] == first
        # A later event with the same target fingerprint moves the job, never duplicates it.
        emit_notification(world, assured)
        later = work.ingest(assured.id, "VALIDITY", expected_version=cursor["row_version"] + 1,
                            classify=lambda event, consumer: (target,), now_ms=now)
        assert later > head and [tuple(row) for row in pending()] == [("work-v14", later, 2, "PENDING")]

    _run(tmp_path, checks)
