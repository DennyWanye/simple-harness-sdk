# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""P2.3c part 3a: the root ``MISSION_FINAL`` review, cut and re-cut.

Part 2d's real-model smoke ended on ``ROOT_REVIEW_PACKAGE_MISSING`` — the root
resolution trigger *reads* three anchors and nothing in ``src`` produced them, so a
hierarchical Mission could never reach ``COMPLETED``.  This file is the coordinator
that produces them, and every test here is about one of two properties:

* **the system never writes the verdict** (AER I05, §21.5 "wrongly declared complete
  = 0").  A cut writes no record at all; a record only ever carries what a reviewer
  said; a reviewer who says ``FAIL`` produces a refused Mission, not a retried one;
* **a cut is a promise about a world, and worlds move**.  A leaf accepted or revoked
  after the cut makes the package stale, the old package is superseded *with a
  record* and a new one is cut — a bounded number of times, after which the Mission
  takes part 2d's idle-stall path instead of spending a model call every cycle.

The reviewer is a fixture here: every reply is scripted, so what is under test is the
coordinator and never a model's wording.  The real model runs in
``test_real_provider_hierarchical_smoke.py``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_htn_end_to_end import (  # noqa: E402
    ROOT_DUTY,
    ROOT_TASK,
    World,
    _accept_every_child,
    _accept_leaf,
    _Artifact,
    _assembly,
    _passing_layers,
    _review_task,
    _revoke,
    committed,
)

from agent_orchestrator.contracts.evidence_state import Validity, WitnessPurpose  # noqa: E402
from agent_orchestrator.contracts.models import ContractError  # noqa: E402
from agent_orchestrator.contracts.resolution import (  # noqa: E402
    CriterionVerdict,
    ReviewAccount,
    ReviewPurpose,
    ReviewVerdict,
)
from agent_orchestrator.contracts.semantic_base import TypedRefKind  # noqa: E402
from agent_orchestrator.orchestrator.root_review import (  # noqa: E402
    DEFAULT_MAX_CUTS_PER_REVISION,
    ROOT_REVIEW_CUT,
    ROOT_REVIEW_CUT_BUDGET_SPENT,
    ROOT_REVIEW_POLICY,
    ROOT_REVIEW_REJECTED,
    ROOT_REVIEW_SUPERSEDED,
    ROOT_REVIEW_UNREADABLE,
    RootReviewCoordinator,
    RootReviewStatus,
    root_criteria,
)

NOW_MS = 2_000_000
#: The root goal type of the shared fixture declares exactly this coverage criterion.
ROOT_CRITERION = "c-root"
REVIEWER = "agent-final-reviewer"


def coordinator(world: World, **kwargs: Any) -> RootReviewCoordinator:
    return RootReviewCoordinator(world.store, world.service, world.dispatch, **kwargs)


def _seeded(tmp_path, *, key: str) -> World:
    """A committed plan whose every gating child has been accepted for real."""

    world = committed(tmp_path, key=key, demand=True)
    world.dispatch.issue_input_witnesses(world.mission.id, world.network(), now_ms=1_000_000)
    _accept_every_child(world)
    return world


@pytest.fixture
def ready(tmp_path) -> World:
    return _seeded(tmp_path, key="p23c-root-review")


@pytest.fixture
def cut(ready: World) -> World:
    coordinator(ready).cut(ready.mission.id, now_ms=NOW_MS)
    return ready


def review(
    world: World,
    *,
    verdict: ReviewVerdict = ReviewVerdict.ACCEPT,
    verdicts: dict[str, CriterionVerdict] | None = None,
    reviewer: str = REVIEWER,
    turn: str = "turn-final-1",
    findings: tuple[dict[str, Any], ...] = (),
):
    """The scripted reviewer's conclusion, handed in the way the collector hands it."""

    coordination = coordinator(world)
    package = coordination.live_package(world.mission.id)
    assert package is not None
    return coordination.record_review(
        world.mission.id,
        package,
        verdict=verdict,
        criterion_verdicts=(
            verdicts if verdicts is not None else {ROOT_CRITERION: CriterionVerdict.PASS}
        ),
        reviewer_agent_id=reviewer,
        reviewer_turn_id=turn,
        findings=findings,
    )


def offer_root(world: World, **kwargs: Any):
    from agent_orchestrator.orchestrator.plan_commits import PlanPrincipal

    return world.dispatch.attempt_root_resolution(
        world.mission.id,
        principal=PlanPrincipal(
            "manager-1", "mission", world.semantics.epoch(world.mission.id, "mission")
        ),
        command_id=f"{world.mission.id}:root-resolution",
        **kwargs,
    )


def events(world: World, kind: str) -> list[Any]:
    return [item for item in world.store.list_events(world.mission.id) if item.type == kind]


# ======================================================================================
# 1. Before the cut: the blocker part 2d's smoke stopped on
# ======================================================================================


def test_the_root_resolution_has_no_package_to_read_before_the_cut(ready: World) -> None:
    """The exact refusal the real-model smoke ended three rounds on."""

    inputs = ready.dispatch.root_resolution_inputs(ready.mission.id)
    assert inputs.reason == "ROOT_REVIEW_PACKAGE_MISSING"
    assert coordinator(ready).state(ready.mission.id).status is RootReviewStatus.CUT_REQUIRED


def test_no_review_is_cut_while_a_gating_child_is_unaccepted(tmp_path) -> None:
    world = committed(tmp_path, key="p23c-root-early", demand=True)
    world.dispatch.issue_input_witnesses(world.mission.id, world.network(), now_ms=1_000_000)
    _accept_leaf(world)  # one of the two gating children only
    state = coordinator(world).state(world.mission.id)
    assert state.status is RootReviewStatus.NOT_READY
    with pytest.raises(ContractError, match="NOT_READY"):
        coordinator(world).cut(world.mission.id, now_ms=NOW_MS)
    assert (
        world.semantics.list_review_packages(world.mission.id, purpose=ReviewPurpose.MISSION_FINAL)
        == ()
    )


def test_the_criteria_of_a_root_review_are_the_root_goals_own(ready: World) -> None:
    binding = ready.semantics.task_semantics_of(ready.mission.id, ROOT_TASK)
    assert binding is not None
    criteria = root_criteria(binding)
    assert [item.criterion_id for item in criteria] == [ROOT_CRITERION]


def test_a_root_criterion_is_judged_and_never_re_executed(ready: World) -> None:
    """A composition review is a judgement; naming a check id would be a forged receipt."""

    from agent_orchestrator.contracts.resolution import EvaluationKind

    binding = ready.semantics.task_semantics_of(ready.mission.id, ROOT_TASK)
    assert binding is not None
    for item in root_criteria(binding):
        assert item.evaluation_kind is EvaluationKind.SEMANTIC
        assert item.required_evidence_policy.required_check_ids == ()
        assert item.required_evidence_policy.independence_required is True


def test_a_root_that_owes_nothing_refuses_to_be_reviewed(ready: World) -> None:
    import dataclasses

    binding = ready.semantics.task_semantics_of(ready.mission.id, ROOT_TASK)
    assert binding is not None
    empty = dataclasses.replace(
        binding,
        goal_signature=dataclasses.replace(binding.goal_signature, coverage_criteria=()),
        requirement_refs=(),
    )
    with pytest.raises(ContractError, match="nothing for a final review to judge"):
        root_criteria(empty)


# ======================================================================================
# 2. The cut: an anchor, a licence, and deliberately no conclusion
# ======================================================================================


def test_the_cut_stores_a_mission_final_package_bound_to_the_root(cut: World) -> None:
    package = coordinator(cut).live_package(cut.mission.id)
    assert package is not None
    assert package.purpose is ReviewPurpose.MISSION_FINAL
    assert str(package.binding.subject_ref.id) == ROOT_TASK
    assert str(package.binding.obligation_id) == ROOT_DUTY
    assert str(package.binding.policy_ref.id) == ROOT_REVIEW_POLICY
    assert cut.semantics.get_review_package(str(package.package_id)) == package


def test_the_cut_binds_the_requirements_revision_it_published(cut: World) -> None:
    package = coordinator(cut).live_package(cut.mission.id)
    assert package is not None
    latest = cut.semantics.latest_requirements_revision(cut.mission.id)
    assert latest is not None
    assert int(package.binding.requirements_revision) == int(latest.revision)
    assert package.requirements_content_hash == latest.content_hash()


def test_the_cut_names_every_contributing_acceptance(cut: World) -> None:
    package = coordinator(cut).live_package(cut.mission.id)
    assert package is not None
    stored = {
        str(item.acceptance_id)
        for item in cut.semantics.list_acceptances(cut.mission.id)
        if item.validity is Validity.CURRENT
    }
    named = {str(item.id) for item in package.child_acceptance_refs}
    assert named == stored
    assert named == {str(item.id) for item in package.candidate_refs}
    assert all(item.kind is TypedRefKind.ACCEPTANCE for item in package.candidate_refs)


def test_the_cut_records_the_producers_the_leaves_own_packages_named(cut: World) -> None:
    """Authorship is read off the frozen leaf anchors, never re-derived here."""

    package = coordinator(cut).live_package(cut.mission.id)
    assert package is not None
    assert package.producer_agent_ids == ("agent-worker",)


def test_the_cut_issues_the_accept_licence_the_root_commit_consumes(cut: World) -> None:
    witnesses = [
        item
        for item in cut.semantics.list_validity_witnesses(cut.mission.id)
        if item.purpose is WitnessPurpose.ACCEPT
        and item.consumer_ref.kind is TypedRefKind.TASK
        and item.consumer_ref.id == ROOT_TASK
    ]
    assert len(witnesses) == 1
    assert witnesses[0].support_revision == len(cut.semantics.list_acceptances(cut.mission.id))


def test_the_cut_writes_no_review_record(cut: World) -> None:
    """The property this whole module exists for: cutting is not concluding (AER I05)."""

    package = coordinator(cut).live_package(cut.mission.id)
    assert package is not None
    assert cut.semantics.official_review_record(str(package.package_id)) is None
    assert coordinator(cut).state(cut.mission.id).status is RootReviewStatus.AWAITING_REVIEW
    outcome = offer_root(cut)
    assert not outcome.committed
    assert outcome.reason == "ROOT_REVIEW_RECORD_MISSING"


def test_the_cut_event_names_everything_it_was_made_over(cut: World) -> None:
    recorded = events(cut, ROOT_REVIEW_CUT)
    assert len(recorded) == 1
    payload = recorded[0].payload
    package = coordinator(cut).live_package(cut.mission.id)
    assert package is not None
    assert payload["package_id"] == str(package.package_id)
    assert payload["review_account"] == str(ReviewAccount.MISSION)
    assert payload["requirements_revision"] == int(package.binding.requirements_revision)
    assert sorted(payload["contributions"]) == sorted(
        str(item.id) for item in package.child_acceptance_refs
    )
    assert payload["superseded"] is None
    assert payload["recut_reasons"] == []


def test_the_review_account_is_the_mission_and_never_a_task(cut: World) -> None:
    """§13 v1.4: a MISSION_FINAL review's cost is the Mission's, not the last leaf's."""

    assert coordinator(cut).account is ReviewAccount.MISSION
    package = coordinator(cut).live_package(cut.mission.id)
    assert package is not None
    row = cut.store.connection.execute(
        "SELECT review_account FROM review_packages WHERE package_id = ?",
        (str(package.package_id),),
    ).fetchone()
    assert row[0] == str(ReviewAccount.MISSION)


def test_a_second_cut_over_a_live_and_fresh_package_is_refused(cut: World) -> None:
    with pytest.raises(ContractError, match="AWAITING_REVIEW"):
        coordinator(cut).cut(cut.mission.id, now_ms=NOW_MS)
    assert len(events(cut, ROOT_REVIEW_CUT)) == 1


def test_the_reviewer_is_shown_the_criteria_and_the_contributions(cut: World) -> None:
    coordination = coordinator(cut)
    package = coordination.live_package(cut.mission.id)
    assert package is not None
    request = coordination.request(cut.mission.id, package)
    assert request.criterion_ids == (ROOT_CRITERION,)
    assert {item["acceptance_id"] for item in request.contributions} == {
        str(item.id) for item in package.child_acceptance_refs
    }
    payload = request.to_json()
    assert payload["budget_account"] == str(ReviewAccount.MISSION)
    # Nothing the reply could steer the system with travels in the context.
    assert not {"mission_id", "principal", "principal_id", "scope_id", "manager_epoch"} & set(
        payload
    )


def test_the_reviewer_is_shown_what_each_contribution_delivered(cut: World) -> None:
    """Part 3a round-3 smoke: every contribution arrived as "no evidence at all".

    The request showed ``acceptance.artifact_refs``, which the accept path does not
    populate — it records the artifact against the **output port** the plan declared,
    in ``acceptance_outputs``.  So a correct reviewer answered FAIL on four accepted
    leaves that had in fact delivered four artifacts.  What a contribution delivered,
    the goal it was delivered against, and the judgement it was accepted under all
    travel now, each read off a stored anchor.
    """

    coordination = coordinator(cut)
    package = coordination.live_package(cut.mission.id)
    contributions = coordination.request(cut.mission.id, package).contributions
    assert contributions
    delivered = [item for item in contributions if item["accepted_outputs"]]
    assert delivered, "a leaf that produced an output at a declared port shows it"
    for item in delivered:
        for output in item["accepted_outputs"]:
            assert output["port"] and output["artifact_id"]
    # Review round 4, P1-2: "at least one" was the half that let the defect survive.
    # On this very fixture one of the two contributions arrived with *no* evidence at
    # all, and a correct reviewer can only answer met=false to that — an error
    # declared the wrong way, at exactly the place §21.5 scores.  **Every**
    # contribution now either carries evidence or says in so many words that it has
    # none, and why.
    for item in contributions:
        assert item["evidence"]["kind"] in {"accepted_outputs", "artifacts", "none"}
        if item["accepted_outputs"] or item["artifacts"]:
            assert item["evidence"]["count"] >= 1
        else:
            assert item["evidence"]["kind"] == "none"
            assert item["evidence"]["reason"], "a blank field is what the reviewer guessed at"
            assert item["goal_statement"] or item["review"], (
                "with no delivered artifact there must still be something to judge on"
            )
    judged = [item for item in contributions if item["review"]]
    assert judged, "and the judgement it was accepted under"
    for item in judged:
        assert item["review"]["verdict"]
        assert item["review"]["review_record_id"]


def test_the_prompt_names_the_fields_the_request_actually_carries(cut: World) -> None:
    """P1-2: the third round fixed the payload and left the prompt pointing at ``artifacts``.

    ``artifacts`` reads ``acceptance.artifact_refs``, which the accept path does not
    populate — so the reviewer was being told to look at the one field that is always
    empty.  A prompt that names a field the request does not deliver is the same
    defect as a request that does not deliver the field.
    """

    from agent_orchestrator.runtime.role_templates import ROOT_REVIEWER

    coordination = coordinator(cut)
    package = coordination.live_package(cut.mission.id)
    contributions = coordination.request(cut.mission.id, package).contributions
    assert contributions
    for field in ("goal_statement", "accepted_outputs", "review", "evidence"):
        assert field in contributions[0], field
        assert field in ROOT_REVIEWER.instructions, field
    assert "artifacts）" not in ROOT_REVIEWER.instructions, (
        "the prompt no longer sends the reviewer to the field the accept path leaves empty"
    )


def test_a_judgement_the_library_cannot_produce_is_an_empty_field(cut: World) -> None:
    """Best effort on the *decoration*: a missing record leaves the field empty.

    The anchors — the package, the acceptances, the criteria — are read strictly; a
    contribution's quoted judgement is context, and an unreadable one must not make
    the whole request unbuildable.
    """

    coordination = coordinator(cut)
    assert coordination._child_review("") == {}
    assert coordination._child_review("review-nobody-wrote") == {}


# ======================================================================================
# 3. The conclusion: the reviewer's, and only the reviewer's
# ======================================================================================


def test_a_pass_becomes_an_accept_record_and_makes_the_root_ready(cut: World) -> None:
    record = review(cut)
    assert record.verdict is ReviewVerdict.ACCEPT
    assert cut.semantics.official_review_record(str(record.package_id)) == record
    assert coordinator(cut).state(cut.mission.id).status is RootReviewStatus.READY


def test_a_fail_is_recorded_announced_and_resolves_nothing(cut: World) -> None:
    record = review(
        cut,
        verdict=ReviewVerdict.REJECTED,
        verdicts={ROOT_CRITERION: CriterionVerdict.FAIL},
        findings=({"severity": "blocker", "detail": "the report never ran the suite"},),
    )
    assert record.verdict is ReviewVerdict.REJECTED
    state = coordinator(cut).state(cut.mission.id)
    assert state.status is RootReviewStatus.REVIEW_REJECTED
    announced = events(cut, ROOT_REVIEW_REJECTED)
    assert len(announced) == 1
    assert announced[0].payload["code"] == "root_review_not_accepted"
    assert announced[0].payload["verdict"] == str(ReviewVerdict.REJECTED)
    assert announced[0].payload["criteria"] == {ROOT_CRITERION: str(CriterionVerdict.FAIL)}
    outcome = offer_root(cut)
    assert not outcome.committed
    assert cut.semantics.adopted_goal_resolution(cut.mission.id, ROOT_DUTY) is None


def test_a_criterion_the_reviewer_did_not_judge_is_unknown(cut: World) -> None:
    record = review(cut, verdicts={})
    assert [item.verdict for item in record.criteria] == [CriterionVerdict.UNKNOWN]
    # And UNKNOWN does not pass: the success formula answers, not this side.
    outcome = offer_root(cut)
    assert not outcome.committed
    assert outcome.reason == "NOT_ACCEPTABLE"


def test_a_second_conclusion_on_one_anchor_is_refused(cut: World) -> None:
    review(cut)
    with pytest.raises(ContractError, match="already carries official record"):
        review(cut, verdict=ReviewVerdict.REJECTED, turn="turn-final-2")


def test_replaying_the_same_conclusion_is_not_a_second_one(cut: World) -> None:
    first = review(cut)
    again = review(cut)
    assert again == first
    assert len(cut.semantics.list_review_records(str(first.package_id))) == 1


def test_an_unreadable_reply_writes_no_record_and_says_so(cut: World) -> None:
    coordination = coordinator(cut)
    package = coordination.live_package(cut.mission.id)
    assert package is not None
    coordination.record_unreadable(
        cut.mission.id, package, detail="critic verdict unreadable", reviewer_turn_id="turn-x"
    )
    assert cut.semantics.official_review_record(str(package.package_id)) is None
    recorded = events(cut, ROOT_REVIEW_UNREADABLE)
    assert len(recorded) == 1
    assert recorded[0].payload["code"] == "root_review_unreadable"
    assert coordinator(cut).state(cut.mission.id).status is RootReviewStatus.AWAITING_REVIEW


def test_the_reviewer_named_on_the_record_is_the_one_that_answered(cut: World) -> None:
    record = review(cut, reviewer="agent-some-other-critic")
    assert record.reviewer_agent_id == "agent-some-other-critic"


def test_a_reviewer_that_produced_the_work_cannot_pass_it(cut: World) -> None:
    """AER §5.3: the independence floor, enforced by the formula and not by hope."""

    review(cut, reviewer="agent-worker")
    outcome = offer_root(cut)
    assert not outcome.committed
    assert outcome.reason == "NOT_ACCEPTABLE"
    assert "INDEPENDENT_REVIEW_MISSING" in outcome.detail


# ======================================================================================
# 4. The root resolution, formed from a real cut and a real review
# ======================================================================================


def test_the_root_resolution_is_formed_from_the_coordinators_anchors(cut: World) -> None:
    """End to end for the blocker: cut → review → ``commit_goal_resolution``."""

    record = review(cut)
    outcome = offer_root(cut)
    assert outcome.committed, f"{outcome.reason}: {outcome.detail}"
    stored = cut.semantics.adopted_goal_resolution(cut.mission.id, ROOT_DUTY)
    assert stored is not None
    assert str(stored.review_receipt_id) == str(record.record_id)
    package = coordinator(cut).live_package(cut.mission.id)
    assert package is not None
    assert int(stored.requirements_version) == int(package.binding.requirements_revision)
    assert coordinator(cut).state(cut.mission.id).status is RootReviewStatus.ALREADY_RESOLVED


def test_the_resolution_restates_the_reviewers_verdict_criterion_by_criterion(
    cut: World,
) -> None:
    review(cut)
    assert offer_root(cut).committed
    stored = cut.semantics.adopted_goal_resolution(cut.mission.id, ROOT_DUTY)
    assert stored is not None
    assert {str(item.criterion_id): item.verdict for item in stored.criteria} == {
        ROOT_CRITERION: CriterionVerdict.PASS
    }


def test_the_mission_duty_is_terminal_once_the_root_is_resolved(cut: World) -> None:
    review(cut)
    assert offer_root(cut).committed
    assert cut.dispatch.terminal(cut.mission.id) is True


# ======================================================================================
# 5. Re-cutting: a cut is a promise about a world, and worlds move
# ======================================================================================


def _accept_one_more_leaf(world: World) -> None:
    """A second acceptance of the review leaf — the shape part 2d's P1-7 describes."""

    _assembly(world).accept(
        world.mission.id,
        _review_task(world),
        result_id="result-review-2",
        layers=_passing_layers(),
        artifacts=(_Artifact("artifact-review-2", "out/verdict.json"),),
        producer_agent_ids=("agent-worker",),
        reviewer_agent_id="agent-critic",
        now_ms=NOW_MS + 100_000,
    )


def test_a_leaf_accepted_after_the_cut_makes_the_package_stale(cut: World) -> None:
    _accept_one_more_leaf(cut)
    state = coordinator(cut).state(cut.mission.id)
    assert state.status is RootReviewStatus.RECUT_REQUIRED
    assert "REQUIREMENTS_MOVED" in state.stale_reasons


def test_the_stale_package_is_exactly_what_the_root_commit_would_refuse(cut: World) -> None:
    """The re-cut rule is not a second opinion: it is the commit's own refusal, earlier."""

    review(cut)
    _accept_one_more_leaf(cut)
    outcome = offer_root(cut)
    assert not outcome.committed
    assert outcome.reason == "READ_SET_STALE"
    assert coordinator(cut).state(cut.mission.id).status is RootReviewStatus.RECUT_REQUIRED


def test_a_recut_supersedes_the_old_package_with_a_record(cut: World) -> None:
    first = coordinator(cut).live_package(cut.mission.id)
    assert first is not None
    _accept_one_more_leaf(cut)
    second = coordinator(cut).cut(cut.mission.id, now_ms=NOW_MS + 200_000)
    assert str(second.package_id) != str(first.package_id)
    retired = events(cut, ROOT_REVIEW_SUPERSEDED)
    assert [item.payload["package_id"] for item in retired] == [str(first.package_id)]
    assert "REQUIREMENTS_MOVED" in retired[0].payload["reasons"]
    # The retired anchor is still *stored* — an anchor is never rewritten — it is
    # simply no longer the one this Mission resolves from.
    assert cut.semantics.get_review_package(str(first.package_id)) == first
    assert coordinator(cut).live_package(cut.mission.id) == second


def test_the_recut_binds_the_new_revision_and_the_new_contributions(cut: World) -> None:
    first = coordinator(cut).live_package(cut.mission.id)
    assert first is not None
    _accept_one_more_leaf(cut)
    second = coordinator(cut).cut(cut.mission.id, now_ms=NOW_MS + 200_000)
    assert int(second.binding.requirements_revision) > int(first.binding.requirements_revision)
    latest = cut.semantics.latest_requirements_revision(cut.mission.id)
    assert latest is not None
    assert int(second.binding.requirements_revision) == int(latest.revision)
    assert {str(item.id) for item in second.child_acceptance_refs} == {
        str(item.acceptance_id)
        for item in cut.semantics.list_acceptances(cut.mission.id)
        if item.validity is Validity.CURRENT
    }
    assert events(cut, ROOT_REVIEW_CUT)[-1].payload["superseded"] == str(first.package_id)


def test_the_root_resolves_after_a_recut_and_a_second_review(cut: World) -> None:
    """The whole repair path, end to end: stale → re-cut → review again → resolved."""

    review(cut)
    _accept_one_more_leaf(cut)
    assert offer_root(cut).reason == "READ_SET_STALE"
    coordinator(cut).cut(cut.mission.id, now_ms=NOW_MS + 200_000)
    review(cut, turn="turn-final-2")
    outcome = offer_root(cut)
    assert outcome.committed, f"{outcome.reason}: {outcome.detail}"
    second = coordinator(cut).live_package(cut.mission.id)
    assert second is not None
    stored = cut.semantics.adopted_goal_resolution(cut.mission.id, ROOT_DUTY)
    assert stored is not None
    assert int(stored.requirements_version) == int(second.binding.requirements_revision)


def test_a_recut_carries_the_licence_over_the_new_support(cut: World) -> None:
    before = {
        item.witness_id
        for item in cut.semantics.list_validity_witnesses(cut.mission.id)
        if item.purpose is WitnessPurpose.ACCEPT and item.consumer_ref.id == ROOT_TASK
    }
    _accept_one_more_leaf(cut)
    coordinator(cut).cut(cut.mission.id, now_ms=NOW_MS + 200_000)
    after = {
        item.witness_id
        for item in cut.semantics.list_validity_witnesses(cut.mission.id)
        if item.purpose is WitnessPurpose.ACCEPT and item.consumer_ref.id == ROOT_TASK
    }
    assert after > before, (
        "a licence is taken over a support; more support is a new licence, not a reuse "
        "of the old TRUE (§11.5, I19)"
    )


# ======================================================================================
# 6. The bound: re-cutting is not free and does not go on for ever
# ======================================================================================


def _churn(world: World) -> None:
    """Make the live package stale **without** moving the requirements revision.

    Re-opening the mission scope is the case the per-revision bound exists for: the
    Mission's requirements are exactly where they were, so the next cut re-uses the
    same revision (``_requirements`` re-publishes only when the content changes) and
    its budget is the one that runs out.  It is also a real refusal and not a
    contrivance — every witness taken in the old epoch is spent (§11.5, I19), so a
    review cut before the bump is a review whose licence no longer holds.
    """

    before = world.semantics.epoch(world.mission.id, "mission")
    while world.semantics.epoch(world.mission.id, "mission") == before:
        # The first bump of a scope that has no row yet writes epoch 0, which is the
        # value it already had; the loop is what makes this helper actually move it.
        world.semantics.bump_epoch(world.mission.id, "mission", bumped_by="test-churn")


def test_a_revoked_contribution_takes_the_root_review_back_to_not_ready(cut: World) -> None:
    """A child nobody accepts any more is not a review to re-cut — it is unfinished work.

    Worth pinning because the tempting answer is "re-cut over the smaller set": the
    root review may only run once every gating child has an accepted outcome, so a
    revoked contribution is a Mission with work left, not a Mission with a stale
    review.  ``root_review_ready`` answers that, and the coordinator quotes it.
    """

    current = [
        item
        for item in cut.semantics.list_acceptances(cut.mission.id)
        if item.validity is Validity.CURRENT
    ]
    _revoke(cut, str(current[-1].acceptance_id))
    assert coordinator(cut).state(cut.mission.id).status is RootReviewStatus.NOT_READY
    assert not offer_root(cut).committed


def test_a_reopened_scope_asks_for_a_recut_at_the_same_revision(cut: World) -> None:
    before = coordinator(cut).live_package(cut.mission.id)
    assert before is not None
    _churn(cut)
    state = coordinator(cut).state(cut.mission.id)
    assert state.status is RootReviewStatus.RECUT_REQUIRED
    assert state.stale_reasons == ("SCOPE_EPOCH_MOVED",)
    after = coordinator(cut).cut(cut.mission.id, now_ms=NOW_MS + 100_000)
    assert int(after.binding.requirements_revision) == int(before.binding.requirements_revision), (
        "nothing published a requirements revision, so the re-cut binds the same one"
    )


def test_one_revision_may_not_be_cut_more_than_the_bound(cut: World) -> None:
    coordination = coordinator(cut, max_cuts_per_revision=2)
    _churn(cut)
    coordination.cut(cut.mission.id, now_ms=NOW_MS + 100_000)
    _churn(cut)
    state = coordination.state(cut.mission.id)
    assert state.status is RootReviewStatus.CUT_BUDGET_SPENT
    assert state.cuts_used == 2
    with pytest.raises(ContractError, match="CUT_BUDGET_SPENT"):
        coordination.cut(cut.mission.id, now_ms=NOW_MS + 200_000)
    assert len(events(cut, ROOT_REVIEW_CUT)) == 2


def test_the_spent_budget_is_recorded_once_per_revision(cut: World) -> None:
    coordination = coordinator(cut, max_cuts_per_revision=1)
    _churn(cut)
    state = coordination.state(cut.mission.id)
    assert state.status is RootReviewStatus.CUT_BUDGET_SPENT
    coordination.record_cut_budget_spent(cut.mission.id, state)
    coordination.record_cut_budget_spent(cut.mission.id, coordination.state(cut.mission.id))
    recorded = events(cut, ROOT_REVIEW_CUT_BUDGET_SPENT)
    assert len(recorded) == 1
    assert recorded[0].payload["bound"] == 1
    assert recorded[0].payload["stale_reasons"] == ["SCOPE_EPOCH_MOVED"]


def test_the_default_bound_is_the_configured_one() -> None:
    from agent_orchestrator.runtime.assembly import OrchestratorConfig

    assert DEFAULT_MAX_CUTS_PER_REVISION == 3
    assert (
        OrchestratorConfig(evidence_root=Path("/tmp/unused")).max_root_review_cuts
        == DEFAULT_MAX_CUTS_PER_REVISION
    )
    with pytest.raises(ValueError, match="max_root_review_cuts"):
        OrchestratorConfig(evidence_root=Path("/tmp/unused"), max_root_review_cuts=0)


def test_a_spent_budget_leaves_no_fresh_package_to_resolve_from(cut: World) -> None:
    """The stuck end is visible, not silent: nothing resolves and the reason is stored."""

    review(cut)
    coordination = coordinator(cut, max_cuts_per_revision=1)
    _churn(cut)
    state = coordination.state(cut.mission.id)
    coordination.record_cut_budget_spent(cut.mission.id, state)
    assert cut.semantics.adopted_goal_resolution(cut.mission.id, ROOT_DUTY) is None
    assert events(cut, ROOT_REVIEW_CUT_BUDGET_SPENT)[0].payload["requirements_revision"] == int(
        state.requirements_revision
    )


# ======================================================================================
# 7. Mutation self-checks: each one is a way this could have been written wrongly
# ======================================================================================


def test_mutant_a_coordinator_that_fills_in_pass_would_declare_a_failed_mission_complete(
    cut: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mutation 1 — the system writes the verdict.

    The guard is
    ``test_a_fail_is_recorded_announced_and_resolves_nothing``: with the mutant in
    place a reviewer that said FAIL produces an ACCEPT record, the Mission resolves,
    and §21.5's "wrongly declared complete = 0" is violated.  The mutant has to be
    *seen* to succeed, not merely to be refused — otherwise the guard would be
    passing for the wrong reason.
    """

    from agent_orchestrator.orchestrator import root_review as module

    real = module.RootReviewCoordinator.record_review

    def always_accepts(self, mission_id, package, **kwargs):  # type: ignore[no-untyped-def]
        kwargs["verdict"] = ReviewVerdict.ACCEPT
        kwargs["criterion_verdicts"] = {ROOT_CRITERION: CriterionVerdict.PASS}
        return real(self, mission_id, package, **kwargs)

    monkeypatch.setattr(module.RootReviewCoordinator, "record_review", always_accepts)
    record = review(
        cut,
        verdict=ReviewVerdict.REJECTED,
        verdicts={ROOT_CRITERION: CriterionVerdict.FAIL},
        findings=({"severity": "blocker", "detail": "nothing was actually run"},),
    )
    assert record.verdict is ReviewVerdict.ACCEPT, "the mutant is in place"
    assert events(cut, ROOT_REVIEW_REJECTED) == [], "the mutant silences the refusal"
    assert offer_root(cut).committed, (
        "the mutant declares a Mission whose reviewer said FAIL complete — which is "
        "exactly what the guard test asserts cannot happen"
    )


def test_mutant_a_recut_that_leaves_the_old_package_live_resolves_from_the_wrong_review(
    cut: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mutation 2 — re-cut without superseding.

    The guard is ``test_a_recut_supersedes_the_old_package_with_a_record``.  Without
    the supersede record both packages stay live, and ``live_package`` — and with it
    the resolution — can answer with the anchor the world has already moved past.
    """

    from agent_orchestrator.orchestrator import root_review as module

    first = coordinator(cut).live_package(cut.mission.id)
    assert first is not None
    monkeypatch.setattr(
        module.RootReviewCoordinator,
        "_supersede",
        lambda self, mission_id, package, *, reasons: None,
    )
    _accept_one_more_leaf(cut)
    second = coordinator(cut).cut(cut.mission.id, now_ms=NOW_MS + 200_000)
    assert events(cut, ROOT_REVIEW_SUPERSEDED) == [], "the mutant is in place"
    stored = cut.semantics.list_review_packages(cut.mission.id, purpose=ReviewPurpose.MISSION_FINAL)
    assert {str(item.package_id) for item in stored} == {
        str(first.package_id),
        str(second.package_id),
    }
    assert coordinator(cut).superseded_package_ids(cut.mission.id) == frozenset(), (
        "two live MISSION_FINAL anchors for one root: which review the Mission is "
        "resolved from is now a matter of row order rather than of judgement"
    )


def test_mutant_an_unbounded_recut_never_stops_asking(
    cut: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mutation 3 — the cut budget is not counted.

    The guard is ``test_one_revision_may_not_be_cut_more_than_the_bound``.  With the
    counter stuck at zero the same requirements revision is cut again every time the
    contributions move, and each cut asks a model on the Mission account — the loop
    part 2d's stall path exists to make visible instead.
    """

    from agent_orchestrator.orchestrator import root_review as module

    monkeypatch.setattr(
        module.RootReviewCoordinator,
        "cuts_for_revision",
        lambda self, mission_id, revision: 0,
    )
    coordination = coordinator(cut, max_cuts_per_revision=1)
    for index in range(3):
        _churn(cut)
        state = coordination.state(cut.mission.id)
        assert state.needs_cut, f"the mutant never runs out of cuts (round {index})"
        assert state.status is not RootReviewStatus.CUT_BUDGET_SPENT
        coordination.cut(cut.mission.id, now_ms=NOW_MS + 100_000 * (index + 1))
    assert events(cut, ROOT_REVIEW_CUT_BUDGET_SPENT) == [], (
        "the bound never bites, so the world can keep moving and the coordinator keeps "
        "asking a model about it on the Mission account"
    )


def test_mutant_a_cut_that_forgets_the_producers_passes_a_self_review(
    ready: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mutation 4 — the cut does not record who produced the work.

    The guard is ``test_a_reviewer_that_produced_the_work_cannot_pass_it``.  The
    independence rule reads authorship off the package, so a package that names
    nobody is a package that says "nobody produced this" — and a reviewer who in fact
    wrote every contribution then passes its own work with nothing to object to.

    Note which half of the rule this mutant reaches.  Leaving the *facts* empty while
    the package still names the producers is already caught, by
    ``FACTS_CONTRADICT_PACKAGE``: the two sides disagree and the formula refuses
    rather than picking one.  Only a mutant that empties **both** — which is what
    forgetting at the cut does, since part 3a reads the facts off the package — turns
    a self-review into an acceptance.
    """

    from agent_orchestrator.orchestrator import root_review as module

    monkeypatch.setattr(
        module.RootReviewCoordinator,
        "producer_agent_ids",
        lambda self, mission_id, acceptances: (),
    )
    coordinator(ready).cut(ready.mission.id, now_ms=NOW_MS)
    package = coordinator(ready).live_package(ready.mission.id)
    assert package is not None
    assert package.producer_agent_ids == (), "the mutant is in place"
    review(ready, reviewer="agent-worker")
    outcome = offer_root(ready)
    assert outcome.committed, (
        "with authorship forgotten at the cut, the producers reviewed their own work "
        "and the Mission was declared complete"
    )


# ======================================================================================
# 8. The loop's own wiring: asking the reviewer, and reading the answer back
# ======================================================================================
#
# Everything above drives the coordinator directly.  ``_ask_root_reviewer`` and
# ``_collect_root_review`` are the two places where the coordinator meets the
# Orchestrator, and they had no test at all — which the part-3a smoke found the hard
# way: ``AgentLimits(max_tool_calls_per_turn=0)`` raised ``ValueError`` on the way to
# creating the intent, so the first Mission that ever reached its own review died
# there.  These tests run that wiring.


def _orchestrator(tmp_path):
    """An Orchestrator over the *same* library file the fixture World is built in."""

    from agent_orchestrator.orchestrator.event_handler import Orchestrator
    from agent_orchestrator.runtime.assembly import OrchestratorConfig
    from agent_orchestrator.testing.fixtures import RoleScriptedProvider

    config = OrchestratorConfig(
        evidence_root=Path(tmp_path), max_concurrency=1, test_timeout_seconds=5
    )
    return Orchestrator(config, RoleScriptedProvider({"planner": []}))


def _asked(world: World, tmp_path):
    """Ask the root reviewer through the loop, and return the intent that was created."""

    import asyncio

    async def case():
        async with _orchestrator(tmp_path) as loop:
            loop.install_hierarchical(planning=world.env)
            mission = loop.store.get_mission(world.mission.id)
            coordination = loop._root_review(mission, loop._new_mode(mission))
            package = coordination.live_package(mission.id)
            assert package is not None
            created = await loop._ask_root_reviewer(mission, coordination, package)
            twice = await loop._ask_root_reviewer(mission, coordination, package)
            subject = f"{mission.id}:root-review:{package.package_id}:1"
            return (
                created,
                twice,
                loop.store.get_intent_for_subject(subject),
                str(package.package_id),
            )

    return asyncio.run(case())


def test_the_loop_really_can_ask_the_root_reviewer(cut: World, tmp_path) -> None:
    """The smoke's ``ValueError``: the limits were rejected before an intent existed."""

    created, _twice, intent, package_id = _asked(cut, tmp_path)
    assert created is True
    assert intent is not None
    assert intent.config["role"] == "root_reviewer"
    assert intent.config["review_package_id"] == package_id


def test_the_reviewer_is_asked_once_per_package(cut: World, tmp_path) -> None:
    created, twice, _intent, _package_id = _asked(cut, tmp_path)
    assert (created, twice) == (True, False), "the creation key is the package id"


def test_the_reviewer_carries_no_tools_and_a_legal_bound(cut: World, tmp_path) -> None:
    """``tool_names=()`` is the gate; the limit is a bound and must be a legal one."""

    _created, _twice, intent, _package_id = _asked(cut, tmp_path)
    limits = intent.config["agent_config"]["limits"]
    assert intent.config["agent_config"]["tool_names"] == []
    assert int(limits["max_tool_calls_per_turn"]) >= 1


def test_the_review_is_charged_to_the_mission_review_account(cut: World, tmp_path) -> None:
    """§13 v1.4: a MISSION_FINAL review is never charged to a Task budget."""

    _created, _twice, intent, _package_id = _asked(cut, tmp_path)
    assert intent.config["budget_account"] == str(ReviewAccount.MISSION)
    # A ``plan`` intent is not bound to an Attempt, so there is no Task budget for it
    # to land on in the first place — which is the structural half of the same rule.
    assert intent.kind == "plan"
    assert intent.subject_id.startswith(f"{cut.mission.id}:root-review:")


def test_mutant_a_reviewer_intent_with_an_illegal_bound_never_reaches_the_store(
    cut: World, tmp_path, monkeypatch
) -> None:
    """The mutation the smoke found: a non-positive tool bound kills the Mission."""

    import asyncio

    from agent_orchestrator.orchestrator import event_handler as module

    real = module.AgentLimits

    def zero(**kwargs: Any):
        return real(**{**kwargs, "max_tool_calls_per_turn": 0})

    monkeypatch.setattr(module, "AgentLimits", zero)

    async def case():
        async with _orchestrator(tmp_path) as loop:
            loop.install_hierarchical(planning=cut.env)
            mission = loop.store.get_mission(cut.mission.id)
            coordination = loop._root_review(mission, loop._new_mode(mission))
            package = coordination.live_package(mission.id)
            with pytest.raises(ValueError):
                await loop._ask_root_reviewer(mission, coordination, package)
            subject = f"{mission.id}:root-review:{package.package_id}"
            return loop.store.get_intent_for_subject(subject)

    assert asyncio.run(case()) is None


def _ask_again(world: World, tmp_path, *, rounds: int):
    """Ask, record an unreadable reply, and ask again — ``rounds`` times."""

    import asyncio

    async def case():
        async with _orchestrator(tmp_path) as loop:
            loop.install_hierarchical(planning=world.env)
            mission = loop.store.get_mission(world.mission.id)
            coordination = loop._root_review(mission, loop._new_mode(mission))
            package = coordination.live_package(mission.id)
            outcomes = []
            for index in range(rounds):
                outcomes.append(await loop._ask_root_reviewer(mission, coordination, package))
                coordination.record_unreadable(
                    mission.id,
                    package,
                    detail="block_missing: no <critic_verdict> block in the output",
                    reviewer_turn_id=f"turn-{index}",
                )
            subjects = [
                loop.store.get_intent_for_subject(
                    f"{mission.id}:root-review:{package.package_id}:{index + 1}"
                )
                for index in range(rounds + 1)
            ]
            return outcomes, subjects

    return asyncio.run(case())


def test_an_unreadable_reply_may_be_put_to_the_reviewer_once_more(cut: World, tmp_path) -> None:
    """The smoke's round-2 ending: the model answered, but not in the frozen shape.

    An unreadable reply is not an answer, so this is the same question put once
    more — with the parse error attached, exactly as the Task Critic's own schema
    retry does.  A reply that *was* read is never re-asked.
    """

    outcomes, subjects = _ask_again(cut, tmp_path, rounds=2)
    assert outcomes == [True, True]
    assert subjects[0] is not None and subjects[1] is not None
    first = json.loads(subjects[0].config["message"]["content"])
    second = json.loads(subjects[1].config["message"]["content"])
    assert "schema_feedback" not in first
    assert "critic_verdict" in second["schema_feedback"]
    assert second["review_package_id"] == first["review_package_id"], "the same anchor"


def test_the_second_unreadable_reply_ends_the_asking(cut: World, tmp_path) -> None:
    """Bounded: a model that cannot produce the block twice is a deployment problem."""

    outcomes, subjects = _ask_again(cut, tmp_path, rounds=3)
    assert outcomes == [True, True, False]
    assert subjects[2] is None, "no third intent was ever created"


def test_a_reviewer_that_answered_is_never_asked_again(cut: World, tmp_path) -> None:
    """A FAIL goes to §9.1's decision table; it does not come back here."""

    review(cut, verdict=ReviewVerdict.REJECTED, verdicts={ROOT_CRITERION: CriterionVerdict.FAIL})
    import asyncio

    async def case():
        async with _orchestrator(tmp_path) as loop:
            loop.install_hierarchical(planning=cut.env)
            mission = loop.store.get_mission(cut.mission.id)
            return await loop._advance_root_review(mission, loop._new_mode(mission))

    assert asyncio.run(case()) is False
    assert events(cut, ROOT_REVIEW_REJECTED), "the refusal is the record, not a retry"


# ======================================================================================
# 9. Reading the reply back: the one line between "the reviewer said FAIL" and COMPLETED
# ======================================================================================
#
# Review round 4, P0-3.  ``_collect_root_review`` had no test at all, and neither did
# the combination a composition review most naturally produces: **FAIL with a blocker
# while every individual criterion is met** — each accepted part satisfies its own
# criterion, and the parts still do not add up to the root goal.  On that shape the
# AER §6.2 success expression *passes* (every criterion is PASS), so
# ``event_handler.py``'s ``ReviewVerdict.ACCEPT if verdict.passed else REJECTED`` is
# the only thing left standing between a FAIL and a Mission declared complete.  The
# review mutated that one line and the whole 2582-test suite stayed green.


def _verdict_block(criteria: list[str], *, verdict: str, met: dict[str, bool] | None = None):
    met = met or {}
    findings = (
        [{"severity": "blocker", "detail": "the parts do not compose into the root goal"}]
        if verdict == "FAIL"
        else []
    )
    return (
        "<critic_verdict>"
        + json.dumps(
            {
                "verdict": verdict,
                "findings": findings,
                "mission_criteria": [
                    {"criterion": item, "met": met.get(item, True), "reason": "scripted"}
                    for item in criteria
                ],
            }
        )
        + "</critic_verdict>"
    )


class _Committed:
    """The shape ``_collect_root_review`` reads a dispatched turn through."""

    def __init__(self, state: Any, turn_id: str = "turn-root-1") -> None:
        self.state = state
        self.turn_id = turn_id
        self.error: dict[str, Any] | None = None


def _collect(world: World, tmp_path, *, verdict: str, met: dict[str, bool] | None = None):
    """Ask the reviewer through the loop, then hand the loop a real reply."""

    import asyncio

    from simple_harness.agents import AgentTurnState

    async def case():
        async with _orchestrator(tmp_path) as loop:
            loop.install_hierarchical(planning=world.env)
            mission = loop.store.get_mission(world.mission.id)
            coordination = loop._root_review(mission, loop._new_mode(mission))
            package = coordination.live_package(mission.id)
            assert package is not None
            await loop._ask_root_reviewer(mission, coordination, package)
            subject = f"{mission.id}:root-review:{package.package_id}:1"
            intent = loop.store.get_intent_for_subject(subject)
            assert intent is not None
            criteria = [str(item) for item in intent.config["review_criteria"]]
            await loop._collect_root_review(
                intent,
                _Committed(AgentTurnState.COMMITTED),
                mission,
                _verdict_block(criteria, verdict=verdict, met=met),
            )
            return (
                criteria,
                coordination.semantics.official_review_record(str(package.package_id)),
                [item for item in loop.store.list_events(mission.id) if "RootReview" in item.type],
            )

    return asyncio.run(case())


def test_a_reviewer_that_said_fail_resolves_nothing_even_with_every_criterion_met(
    cut: World, tmp_path
) -> None:
    """P0-3: the shape a composition review exists to produce, end to end.

    Every criterion ``met: true`` and a blocker on the whole, so the §6.2 success
    expression is satisfied on every axis it evaluates.  The record's own verdict is
    the only remaining defence, and this is the test that stands on it: mutate
    ``event_handler``'s ``ACCEPT if verdict.passed else REJECTED`` to a bare ``ACCEPT``
    and the root ``GoalResolution`` commits.
    """

    criteria, record, _events = _collect(cut, tmp_path, verdict="FAIL")
    assert criteria == [ROOT_CRITERION]
    assert record is not None
    assert record.verdict is ReviewVerdict.REJECTED
    # The criteria axis really is all-PASS: this test is not passing for the wrong reason.
    assert [item.verdict for item in record.criteria] == [CriterionVerdict.PASS]
    outcome = offer_root(cut)
    assert outcome.committed is False
    assert cut.semantics.adopted_goal_resolution(cut.mission.id, ROOT_DUTY) is None


def test_a_reviewer_that_said_pass_is_recorded_as_an_accept(cut: World, tmp_path) -> None:
    """The control for the test above: the same wiring, the opposite conclusion."""

    _criteria, record, _events = _collect(cut, tmp_path, verdict="PASS")
    assert record is not None
    assert record.verdict is ReviewVerdict.ACCEPT
    assert [item.verdict for item in record.criteria] == [CriterionVerdict.PASS]


def test_a_pass_that_names_an_unmet_criterion_is_not_a_conclusion(cut: World, tmp_path) -> None:
    """The symmetric half of P0-3: two judgements that contradict each other.

    ``parse_critic_verdict`` allows it — it only checks ``FAIL ⟺ blocker`` — and the
    legacy Task Critic's §22 contract genuinely permits a PASS that names an unmet
    criterion, so the refusal lives in the root-review coordinator instead of in the
    shared parser.  No official record is written and the package keeps its one slot.
    """

    _criteria, record, recorded = _collect(
        cut, tmp_path, verdict="PASS", met={ROOT_CRITERION: False}
    )
    assert record is None
    assert [item.type for item in recorded if item.type == ROOT_REVIEW_REJECTED] == []
    outcome = offer_root(cut)
    assert outcome.committed is False


def test_the_coordinator_refuses_a_self_contradicting_accept_directly(cut: World) -> None:
    """And at the coordinator's own door, not only through the loop."""

    from agent_orchestrator.orchestrator.root_review import refuse_self_contradicting_accept

    with pytest.raises(ContractError, match="may not also report a criterion it judged FAIL"):
        review(cut, verdict=ReviewVerdict.ACCEPT, verdicts={ROOT_CRITERION: CriterionVerdict.FAIL})
    # …while the legal asymmetric shape is left alone.
    refuse_self_contradicting_accept(
        ReviewVerdict.REJECTED, {ROOT_CRITERION: CriterionVerdict.PASS}
    )
    refuse_self_contradicting_accept(
        ReviewVerdict.ACCEPT, {ROOT_CRITERION: CriterionVerdict.UNKNOWN}
    )


def test_mutant_ignoring_the_reviewers_conclusion_declares_the_mission_complete(
    cut: World, tmp_path, monkeypatch
) -> None:
    """Mutation self-proof for P0-3 (the review's M20), run end to end.

    ``ReviewVerdict.ACCEPT if verdict.passed else REJECTED`` is replaced by a bare
    ACCEPT — the single-line regression the review demonstrated — and the same FAIL
    reply now produces a committed root ``GoalResolution``.  The guard above is the
    only automated evidence that this line is load bearing.
    """

    from agent_orchestrator.orchestrator import root_review as module

    real = module.RootReviewCoordinator.record_review

    def always_accept(self, mission_id, package, **kwargs: Any):
        return real(self, mission_id, package, **{**kwargs, "verdict": ReviewVerdict.ACCEPT})

    monkeypatch.setattr(module.RootReviewCoordinator, "record_review", always_accept)
    _criteria, record, _events = _collect(cut, tmp_path, verdict="FAIL")
    assert record is not None
    assert record.verdict is ReviewVerdict.ACCEPT, "the mutant is in place"
    outcome = offer_root(cut)
    assert outcome.committed is True, (
        "with the reviewer's conclusion ignored the root goal resolves and the Mission "
        "is declared complete on a reply that said FAIL"
    )


# ======================================================================================
# 10. The one parser: a malformed verdict is an error, never a PASS (P1-7)
# ======================================================================================
#
# ``critics.py`` is not touched by this slice, but part 3a made the root MISSION_FINAL
# review depend on it — deliberately, so that there is exactly one place a reply can
# become a PASS (``role_templates.py``: "a second parser is a second place a bad reply
# becomes a PASS").  The review then found that the *whole repository* had no test for
# the negative direction: replacing ``raise ContractError`` with ``verdict = "PASS"``
# left all 2582 tests green.  These are that test.


def _block(payload: dict[str, Any]) -> str:
    return "<critic_verdict>" + json.dumps(payload) + "</critic_verdict>"


@pytest.mark.parametrize(
    ("name", "verdict"),
    [
        ("lower case", "pass"),
        ("a synonym", "ok"),
        ("null", None),
        ("a number", 1),
        ("a list", ["PASS"]),
        ("empty", ""),
    ],
)
def test_a_verdict_that_is_not_pass_or_fail_is_a_contract_error(name: str, verdict: Any) -> None:
    from agent_orchestrator.verification.critics import parse_critic_verdict

    with pytest.raises(ContractError, match="must be PASS or FAIL"):
        parse_critic_verdict(
            _block(
                {
                    "verdict": verdict,
                    "findings": [],
                    "mission_criteria": [{"criterion": "c-1", "met": True}],
                }
            ),
            expected_criteria=["c-1"],
        )


def test_a_reply_with_no_verdict_field_at_all_is_a_contract_error() -> None:
    from agent_orchestrator.verification.critics import parse_critic_verdict

    with pytest.raises(ContractError, match="must be PASS or FAIL"):
        parse_critic_verdict(
            _block({"findings": [], "mission_criteria": [{"criterion": "c-1", "met": True}]}),
            expected_criteria=["c-1"],
        )


def test_a_reply_with_no_block_is_a_contract_error() -> None:
    from agent_orchestrator.verification.critics import parse_critic_verdict

    with pytest.raises(ContractError, match="unreadable"):
        parse_critic_verdict("PASS, everything looks fine", expected_criteria=["c-1"])


@pytest.mark.parametrize(
    ("name", "payload", "match"),
    [
        (
            "a met that is not a bool",
            {
                "verdict": "PASS",
                "findings": [],
                "mission_criteria": [{"criterion": "c-1", "met": "yes"}],
            },
            "met must be boolean",
        ),
        (
            "a FAIL with no blocker",
            {
                "verdict": "FAIL",
                "findings": [{"severity": "minor", "detail": "nit"}],
                "mission_criteria": [{"criterion": "c-1", "met": True}],
            },
            "FAIL iff a blocker",
        ),
        (
            "a PASS with a blocker",
            {
                "verdict": "PASS",
                "findings": [{"severity": "blocker", "detail": "no"}],
                "mission_criteria": [{"criterion": "c-1", "met": True}],
            },
            "FAIL iff a blocker",
        ),
        (
            "criteria in the wrong order",
            {
                "verdict": "PASS",
                "findings": [],
                "mission_criteria": [{"criterion": "c-2", "met": True}],
            },
            "cover the Mission criteria in order",
        ),
    ],
)
def test_every_other_malformed_verdict_is_refused_too(
    name: str, payload: dict[str, Any], match: str
) -> None:
    from agent_orchestrator.verification.critics import parse_critic_verdict

    with pytest.raises(ContractError, match=match):
        parse_critic_verdict(_block(payload), expected_criteria=["c-1"])


def test_mutant_a_parser_that_defaults_to_pass_would_pass_every_malformed_reply() -> None:
    """Mutation self-proof for P1-7 (the review's M03): the guard above is the only one.

    With ``verdict not in {"PASS","FAIL"}`` answering PASS instead of raising, a reply
    saying ``"ok"`` becomes a passing verdict — and since part 3a the root review reads
    its conclusion through this same parser.
    """

    from agent_orchestrator.verification.critics import CriticVerdict

    assert CriticVerdict(verdict="ok", findings=(), mission_criteria=(), raw={}).passed is False, (
        "only the exact word PASS passes"
    )
    assert CriticVerdict(verdict="PASS", findings=(), mission_criteria=(), raw={}).passed is True


# ======================================================================================
# 11. Review round 4, P2: channels and rules that were only ever covered by accident
# ======================================================================================


def test_contributions_moving_is_its_own_recut_channel(cut: World) -> None:
    """P2-3: ``CONTRIBUTIONS_MOVED`` had no test — ``REQUIREMENTS_MOVED`` hid it.

    Every leaf acceptance publishes a new Mission-level ``RequirementsRevision``, so
    in the natural fixture both codes fire together and the existing test asserts the
    other one.  Turning the channel off survived the whole suite.  Here the package
    is made to disagree about its *contributions* only, with the requirements
    revision left exactly where the live package found it.
    """

    import dataclasses

    from agent_orchestrator.orchestrator.root_review import acceptance_ref

    coordination = coordinator(cut)
    package = coordination.live_package(cut.mission.id)
    assert package is not None
    assert coordination.stale_reasons(cut.mission.id, package) == ()
    moved = dataclasses.replace(
        package,
        child_acceptance_refs=(*package.child_acceptance_refs, acceptance_ref("acc-from-later")),
    )
    reasons = coordination.stale_reasons(cut.mission.id, moved)
    assert reasons == ("CONTRIBUTIONS_MOVED",), "only this channel, and it really fires"
    assert int(moved.binding.requirements_revision) == int(package.binding.requirements_revision), (
        "the requirements did not move, so the other channel cannot be what answered"
    )


def test_the_live_package_skips_a_superseded_one_even_when_it_is_the_last(cut: World) -> None:
    """P2-4, rule 2 on its own: the newest package is not automatically the live one.

    Rules 2 (skip superseded) and 3 (take the last recorded cut) are redundant on the
    natural path — a re-cut both supersedes the old package *and* appends a later cut
    event — so the review's mutations of each one separately both survived.  Here the
    **last** recorded cut is the superseded one, which only rule 2 can answer.
    """

    import dataclasses

    from agent_orchestrator.contracts.resolution import ReviewPackageId
    from agent_orchestrator.orchestrator.hierarchical_dispatch import append_hierarchical_event
    from agent_orchestrator.orchestrator.root_review import ROOT_REVIEW_SUPERSEDED

    coordination = coordinator(cut)
    first = coordination.live_package(cut.mission.id)
    assert first is not None
    second = dataclasses.replace(first, package_id=ReviewPackageId(str(first.package_id) + "-b"))
    cut.semantics.insert_review_package(second)
    append_hierarchical_event(
        cut.store,
        ROOT_REVIEW_CUT,
        cut.mission.id,
        key=f"{cut.mission.id}:{second.package_id}",
        task_id=str(second.binding.subject_ref.id),
        payload={
            "package_id": str(second.package_id),
            "requirements_revision": int(second.binding.requirements_revision),
            "scope_epoch": 0,
        },
    )
    assert str(coordinator(cut).live_package(cut.mission.id).package_id) == str(
        second.package_id
    ), "rule 3 alone would pick the later cut"
    append_hierarchical_event(
        cut.store,
        ROOT_REVIEW_SUPERSEDED,
        cut.mission.id,
        key=f"{cut.mission.id}:{second.package_id}",
        task_id=str(second.binding.subject_ref.id),
        payload={"package_id": str(second.package_id), "reasons": ["TEST"]},
    )
    live = coordinator(cut).live_package(cut.mission.id)
    assert live is not None
    assert str(live.package_id) == str(first.package_id), (
        "a retired anchor is never the live one, however recently it was cut"
    )


def test_the_live_package_takes_the_last_cut_of_two_that_are_both_live(cut: World) -> None:
    """P2-4, rule 3 on its own: two packages, neither superseded."""

    import dataclasses

    from agent_orchestrator.contracts.resolution import ReviewPackageId
    from agent_orchestrator.orchestrator.hierarchical_dispatch import append_hierarchical_event

    coordination = coordinator(cut)
    first = coordination.live_package(cut.mission.id)
    assert first is not None
    second = dataclasses.replace(first, package_id=ReviewPackageId(str(first.package_id) + "-c"))
    cut.semantics.insert_review_package(second)
    append_hierarchical_event(
        cut.store,
        ROOT_REVIEW_CUT,
        cut.mission.id,
        key=f"{cut.mission.id}:{second.package_id}",
        task_id=str(second.binding.subject_ref.id),
        payload={
            "package_id": str(second.package_id),
            "requirements_revision": int(second.binding.requirements_revision),
            "scope_epoch": 0,
        },
    )
    live = coordinator(cut).live_package(cut.mission.id)
    assert live is not None
    assert str(live.package_id) == str(second.package_id), (
        "with nothing superseded, the answer is the last cut this deployment recorded"
    )


def test_a_criterion_nobody_judged_is_recorded_as_never_having_been_run(cut: World) -> None:
    """P2-5 / I07: the *execution* axis, which no test read.

    ``project_verdict`` flattens a non-conclusive execution to UNKNOWN anyway, so the
    acceptance answer does not change — but the stored record is what a replay reads,
    and "judged FAIL" and "nobody looked" are different facts about the same
    criterion.  Turning ``NOT_RUN`` into ``SUCCEEDED`` survived the whole suite.
    """

    from agent_orchestrator.contracts.resolution import CheckExecution

    unjudged = review(cut, verdicts={})
    assert [item.check_execution for item in unjudged.criteria] == [CheckExecution.NOT_RUN]
    assert [item.verdict for item in unjudged.criteria] == [CriterionVerdict.UNKNOWN]


@pytest.mark.parametrize(
    ("verdict", "criterion"),
    [
        (ReviewVerdict.REJECTED, CriterionVerdict.FAIL),
        (ReviewVerdict.ACCEPT, CriterionVerdict.PASS),
    ],
)
def test_a_criterion_the_reviewer_did_judge_carries_a_finished_execution(
    cut: World, verdict: ReviewVerdict, criterion: CriterionVerdict
) -> None:
    """Either way round: a judgement that happened is an execution that succeeded."""

    from agent_orchestrator.contracts.resolution import CheckExecution

    record = review(cut, verdict=verdict, verdicts={ROOT_CRITERION: criterion})
    assert [item.check_execution for item in record.criteria] == [CheckExecution.SUCCEEDED]


def test_ready_says_which_half_of_the_question_is_ready(cut: World) -> None:
    """P2-6: ``READY`` is this module's answer, not the Mission's.

    A reviewer that accepts while reporting an unmet criterion leaves the state here
    at READY for ever while ``commit_goal_resolution`` refuses it every cycle, and an
    operator reading a bare "READY" has nothing to go on.
    """

    review(cut)
    state = coordinator(cut).state(cut.mission.id)
    assert state.status is RootReviewStatus.READY
    assert "success expression" in state.detail
    assert "commit_goal_resolution" in state.detail
