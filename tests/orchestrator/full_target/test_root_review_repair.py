# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""P2.3d / defect D5-A: §9.1's repair branch after a root review says REJECT.

Ten of the Grok acceptance run's 40 episodes ended like this:

    HierarchicalRootReviewCut(4 contributions)
    → HierarchicalRootReviewRejected{c-test-passes: FAIL, finding: "…"}
    → RootGoalResolutionRefused{SUCCESS_EXPRESSION_NOT_PASS, REVIEW_VERDICT_NOT_ACCEPT}
    → HierarchicalMissionStalled{hierarchical_no_dispatchable_work}
    → MissionFailed

Every leaf had been dispatched, verified and accepted; in nine of them the official
hidden grader passed the deliverable.  ``_advance_root_review`` recorded the rejection,
noted it, and returned False — the comment said "§9.1's decision table, never a silent
retry", and the decision table itself had never been implemented.  A Mission's whole
plan was decided once and a single review failure was a death sentence.

The minimal branch: a **blocking** finding reopens one Planner round per plan revision,
with the findings travelling as durable ``PlanningRejected`` feedback.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_htn_end_to_end import World, _accept_every_child, committed  # noqa: E402
from test_root_review_coordinator import coordinator, review  # noqa: E402

from agent_orchestrator.contracts import MissionStopReason  # noqa: E402
from agent_orchestrator.contracts.models import MissionStatus  # noqa: E402
from agent_orchestrator.contracts.resolution import (  # noqa: E402
    CriterionVerdict,
    ReviewVerdict,
)
from agent_orchestrator.contracts.state_machines import TERMINAL_MISSION  # noqa: E402
from agent_orchestrator.orchestrator.commit_service import mission_account  # noqa: E402
from agent_orchestrator.orchestrator.event_handler import (  # noqa: E402
    ROOT_REVIEW_REPAIR_REASON,
    Orchestrator,
)
from agent_orchestrator.runtime.assembly import OrchestratorConfig  # noqa: E402
from agent_orchestrator.storage.htn_store import HtnStore  # noqa: E402
from agent_orchestrator.testing.fixtures import RoleScriptedProvider  # noqa: E402

NOW_MS = 2_000_000
ROOT_CRITERION = "c-root"

BLOCKER = (
    {
        "severity": "blocker",
        "criterion_id": ROOT_CRITERION,
        "detail": (
            "c-root has no readable proof: evidence.kind=none, no artifact was "
            "delivered on a declared output port"
        ),
    },
)
MINOR = ({"severity": "minor", "criterion_id": ROOT_CRITERION, "detail": "wording"},)


def _rejected_world(tmp_path, *, findings, key: str, tokens: int | None = None) -> World:
    """A Mission whose root review has concluded REJECT, on disk and closed."""

    evidence = Path(tmp_path) / "evidence"
    evidence.mkdir(parents=True, exist_ok=True)
    world = committed(
        evidence, key=key, demand=True, **({} if tokens is None else {"tokens": tokens})
    )
    world.dispatch.issue_input_witnesses(world.mission.id, world.network(), now_ms=1_000_000)
    _accept_every_child(world)
    coordinator(world).cut(world.mission.id, now_ms=NOW_MS)
    review(
        world,
        verdict=ReviewVerdict.REJECTED,
        verdicts={ROOT_CRITERION: CriterionVerdict.FAIL},
        findings=findings,
    )
    world.store.close()
    return world


def _advance(
    world: World,
    evidence: Path,
    *,
    repairs: int = 1,
    rounds: int = 1,
    spend_ordinal_one: bool = False,
) -> dict[str, Any]:
    """Run ``_advance_root_review`` ``rounds`` times on a real Orchestrator."""

    config = OrchestratorConfig(
        evidence_root=evidence,
        max_concurrency=1,
        test_timeout_seconds=5,
        max_root_review_repairs=repairs,
        max_planning_attempts=4,
    )

    async def case() -> dict[str, Any]:
        async with Orchestrator(config, RoleScriptedProvider({"planner": []})) as loop:
            world.env.semantics = HtnStore(loop.store)
            loop.install_hierarchical(planning=world.env)
            if spend_ordinal_one:
                # Review P2-8: in production the first plan came from a Planner round,
                # so the repair round is ordinal >= 2 — and ``_create_planner_intent``
                # only puts ``_planning_rejections`` into the package when it is.  The
                # fixture commits its first revision directly, so without this the
                # repair round is ordinal 1 and the findings never reach the model.
                await loop._try_planner_intent(world.mission.id, ordinal=1)
                for item in loop.store.list_intents(
                    "PENDING", "CLAIMED", "AGENT_CREATED", "SUBMITTED"
                ):
                    if item.mission_id == world.mission.id and item.kind == "plan":
                        loop._settle_intent(item, "FAILED")
            moved: list[bool] = []
            for _ in range(rounds):
                mission = loop.store.get_mission(world.mission.id)
                assert mission is not None
                moved.append(
                    await loop._advance_root_review(mission, loop._new_mode(mission))
                )
            return {
                "moved": moved,
                "events": list(loop.store.list_events(world.mission.id)),
                "intents": [
                    item
                    for item in loop.store.list_intents(
                        "PENDING", "CLAIMED", "AGENT_CREATED", "SUBMITTED"
                    )
                    if item.mission_id == world.mission.id and item.kind == "plan"
                ],
                "status": loop.store.get_mission(world.mission.id).status,
                "stop_reason": loop.store.get_mission(world.mission.id).stop_reason,
                "next_ordinal": loop._next_planning_ordinal(world.mission.id),
                "report": dict(
                    loop.store.get_mission(world.mission.id).final_report or {}
                ),
                "packages": [
                    str(item.config.get("message", ""))
                    for item in loop.store.list_intents(
                        "PENDING", "CLAIMED", "AGENT_CREATED", "SUBMITTED"
                    )
                    if item.mission_id == world.mission.id and item.kind == "plan"
                ],
            }

    return asyncio.run(case())


@pytest.fixture
def blocked(tmp_path):
    world = _rejected_world(tmp_path, findings=BLOCKER, key="p23d-repair")
    return world, Path(tmp_path) / "evidence"


def test_a_blocking_finding_reopens_one_planner_round(blocked) -> None:
    """The branch that did not exist.

    **Mutation**: return False from ``_repair_after_root_review`` unconditionally and
    the Mission is back to the ten lost episodes — rejection recorded, nothing left to
    dispatch, idle stall.
    """

    world, evidence = blocked
    outcome = _advance(world, evidence)
    assert outcome["moved"] == [True], "a repair round is progress, not idleness"
    # The fixture commits its first revision through ``apply_planner_reply`` rather than
    # through a Planner intent, so ordinal 1 is still free here; what matters is that a
    # *free* one is taken, because the ordinal is the intent's creation key and reusing
    # a spent one would hand back the old intent and dispatch nothing.
    assert [item.config["ordinal"] for item in outcome["intents"]] == [1]
    assert outcome["next_ordinal"] == 2


def test_the_findings_travel_to_the_planner_as_durable_feedback(blocked) -> None:
    """Asking again without saying what was wrong is the silent retry §9.1 forbids."""

    world, evidence = blocked
    outcome = _advance(world, evidence)
    recorded = [
        item
        for item in outcome["events"]
        if item.type == "PlanningRejected"
        and item.payload.get("reason") == ROOT_REVIEW_REPAIR_REASON
    ]
    assert len(recorded) == 1
    detail = recorded[0].payload["detail"]
    assert detail["repair_round"] == 1
    assert detail["plan_revision"] == 1
    assert [item["severity"] for item in detail["findings"]] == ["blocker"]
    assert "no readable proof" in detail["findings"][0]["detail"]


def test_a_second_cycle_on_the_same_revision_does_not_ask_again(blocked) -> None:
    """The bound is per plan revision, so a Mission cannot circle here."""

    world, evidence = blocked
    outcome = _advance(world, evidence, rounds=3)
    assert outcome["moved"] == [True, False, False]
    assert len(outcome["intents"]) == 1
    assert (
        sum(
            1
            for item in outcome["events"]
            if item.type == "PlanningRejected"
            and item.payload.get("reason") == ROOT_REVIEW_REPAIR_REASON
        )
        == 1
    )


def test_a_minor_finding_is_not_a_request_for_a_new_plan(tmp_path) -> None:
    """A reviewer that rejected over wording is not asking for the plan to change.

    Re-planning on that would be this loop deciding the review was wrong, which is the
    silent retry the decision table exists to prevent.
    """

    world = _rejected_world(tmp_path, findings=MINOR, key="p23d-repair-minor")
    outcome = _advance(world, Path(tmp_path) / "evidence")
    assert outcome["moved"] == [False]
    assert outcome["intents"] == []


def test_no_findings_at_all_is_not_a_request_for_a_new_plan(tmp_path) -> None:
    world = _rejected_world(tmp_path, findings=(), key="p23d-repair-none")
    outcome = _advance(world, Path(tmp_path) / "evidence")
    assert outcome["moved"] == [False]
    assert outcome["intents"] == []


def test_the_bound_is_configuration_and_zero_switches_the_branch_off(tmp_path) -> None:
    """``max_root_review_repairs=0`` restores the behaviour the defect describes."""

    world = _rejected_world(tmp_path, findings=BLOCKER, key="p23d-repair-off")
    outcome = _advance(world, Path(tmp_path) / "evidence", repairs=0)
    assert outcome["moved"] == [False]
    assert outcome["intents"] == []


def test_the_config_refuses_a_negative_bound() -> None:
    with pytest.raises(ValueError, match="max_root_review_repairs"):
        OrchestratorConfig(evidence_root=Path("/tmp/none"), max_root_review_repairs=-1)


def test_a_rejected_repair_round_does_not_kill_a_mission_that_holds_a_plan(tmp_path) -> None:
    """The other half of D5-A, and the half that bites hardest.

    ``_planning_rejected`` used to end ``fail_planning`` unconditionally once the
    ladder was spent.  That was sound while the only planning rounds were the ones
    that *produced* the first plan — the Mission was PLANNING, and a Mission that
    cannot be planned is a dead Mission.  D5-A and D5-B both open a round **after** a
    plan is committed and the Mission is ACTIVE, so the old ending turned "the repair
    attempt did not work out" into "the Mission is failed", throwing away a plan whose
    leaves were all accepted.

    **Mutation**: drop the ``mission.status is MissionStatus.PLANNING`` guard and this
    goes red — the Mission comes back FAILED with the repair round's reason.
    """

    world = _rejected_world(tmp_path, findings=BLOCKER, key="p23d-repair-survives")
    evidence = Path(tmp_path) / "evidence"
    config = OrchestratorConfig(
        evidence_root=evidence,
        max_concurrency=1,
        test_timeout_seconds=5,
        max_root_review_repairs=1,
        # One rung: the repair round itself is the last one, so its rejection lands on
        # the branch this test is about instead of opening yet another round.
        max_planning_attempts=1,
    )

    async def case() -> dict[str, Any]:
        async with Orchestrator(config, RoleScriptedProvider({"planner": []})) as loop:
            world.env.semantics = HtnStore(loop.store)
            loop.install_hierarchical(planning=world.env)
            mission = loop.store.get_mission(world.mission.id)
            assert mission is not None
            before = mission.status
            assert await loop._advance_root_review(mission, loop._new_mode(mission))
            intent = next(
                item
                for item in loop.store.list_intents(
                    "PENDING", "CLAIMED", "AGENT_CREATED", "SUBMITTED"
                )
                if item.mission_id == world.mission.id and item.kind == "plan"
            )
            await loop._planning_rejected(
                intent, reason="proposal_unreadable", detail={"error": "no block"}
            )
            return {
                "before": before,
                "after": loop.store.get_mission(world.mission.id).status,
                "events": [
                    item.type for item in loop.store.list_events(world.mission.id)
                ],
            }

    outcome = asyncio.run(case())
    assert outcome["before"] is MissionStatus.ACTIVE, "the fixture holds a committed plan"
    assert outcome["after"] is MissionStatus.ACTIVE
    assert "MissionFailed" not in outcome["events"]
    # The refusal is still written down — not failing is not the same as not noticing.
    # Two records: why the round was opened, and what came back from it (review P2-2).
    assert outcome["events"].count("PlanningRejected") == 2


def test_a_repair_round_that_cannot_be_funded_stops_the_mission_visibly(tmp_path) -> None:
    """Review P0-1, and the worst place in a Mission's life to find it.

    The repair branch opens exactly after four leaves, four critics and one root review
    have been paid for — the moment a Mission's account is emptiest.
    ``_create_planner_intent`` reserves ``planner_reserve_tokens`` and raises
    ``BudgetExhausted`` when the account cannot cover it; ``_cycle`` does not forgive a
    ``StoreError`` and ``run()`` has no outer guard, so the runner's episode died with a
    traceback, its ``PlanningRejected{root_review_rejected}`` already on disk, the
    Mission left ACTIVE and no ``MissionFailed`` at all.  That is strictly worse than
    the 0.12.0 behaviour this branch was written to improve on.

    The Mission already holds a committed plan, so ``fail_planning`` is the wrong
    ending (it files the stop as a *planning* failure on a Mission that was planned):
    the honest one is a Mission-level stop with ``BUDGET_EXHAUSTED``.
    """

    world = _rejected_world(tmp_path, findings=BLOCKER, key="p23d-repair-broke", tokens=100)
    outcome = _advance(world, Path(tmp_path) / "evidence")
    assert outcome["moved"] == [False], "an unfundable round is not progress"
    assert outcome["status"] is MissionStatus.FAILED
    assert outcome["stop_reason"] == str(MissionStopReason.BUDGET_EXHAUSTED)
    assert not outcome["intents"], "nothing was dispatched"
    # Mutations VA and VJ: ``fail_planning(stop_reason=BUDGET_EXHAUSTED)`` produces the
    # same status and the same stop reason, so the two assertions above cannot tell the
    # endings apart — and telling them apart is the whole point of ``_stop_planning_round``.
    # ``fail_planning`` files a ``planning_failure`` on a Mission that *was* planned and
    # leaves its open work running; ``fail_mission`` files the Mission-level stop and
    # cascades.
    assert "planning_failure" not in outcome["report"], (
        "a Mission holding a committed plan did not fail at planning"
    )
    assert outcome["report"].get("detail", {}).get("phase") == "root_review_repair"
    assert "TaskCancelled" in [item.type for item in outcome["events"]], (
        "the open work is stopped with it"
    )


def test_the_whole_loop_survives_a_repair_round_it_cannot_fund(tmp_path) -> None:
    """The property the probe pinned: ``run()`` returns instead of raising.

    A per-Mission stop that escapes ``_cycle`` is a *run*-level failure — every other
    Mission in the process dies with it.  §24.1 decision 11 says one Mission's stop is
    one Mission's stop.
    """

    world = _rejected_world(tmp_path, findings=BLOCKER, key="p23d-repair-loop", tokens=100)
    evidence = Path(tmp_path) / "evidence"
    config = OrchestratorConfig(
        evidence_root=evidence,
        max_concurrency=1,
        test_timeout_seconds=5,
        max_root_review_repairs=1,
        max_planning_attempts=2,
    )

    async def case() -> dict[str, Any]:
        async with Orchestrator(config, RoleScriptedProvider({"planner": []})) as loop:
            world.env.semantics = HtnStore(loop.store)
            loop.install_hierarchical(planning=world.env)
            await loop.run(max_cycles=4)
            final = loop.store.get_mission(world.mission.id)
            return {
                "status": final.status,
                "stop_reason": final.stop_reason,
                "events": [item.type for item in loop.store.list_events(world.mission.id)],
            }

    outcome = asyncio.run(case())
    assert outcome["status"] in TERMINAL_MISSION, "a Mission that cannot go on has an ending"
    assert "MissionFailed" in outcome["events"]


def test_the_findings_are_in_the_package_the_repair_round_actually_carries(blocked) -> None:
    """Review P2-8: the event is not the delivery — the package is.

    ``test_the_findings_travel_to_the_planner_as_durable_feedback`` proves the record
    exists; ``_create_planner_intent`` only folds ``_planning_rejections`` into the
    package when ``ordinal > 1``, so with the fixture's first revision committed
    directly the repair round was ordinal 1 and carried nothing.  Production ordinals
    are >= 2, so the feature worked — but nothing here could have noticed if it stopped.
    """

    world, evidence = blocked
    outcome = _advance(world, evidence, spend_ordinal_one=True)
    assert outcome["moved"] == [True]
    assert outcome["next_ordinal"] == 3, "ordinal 1 is spent, the repair took 2"
    repair = [item for item in outcome["packages"] if "no readable proof" in item]
    assert repair, "the blocking finding never reached the Planner's package"
    assert ROOT_REVIEW_REPAIR_REASON in repair[0]


def test_the_repair_record_does_not_swallow_the_rounds_own_rejection(blocked) -> None:
    """Review P2-2: two different things were sharing one idempotency key.

    ``record_planning_rejected`` is keyed ``{mission}:planner:{ordinal}``, and D5-A
    wrote the findings under the ordinal it was about to open.  When *that* round was
    then refused on its own merits — unreadable, ungrounded — the second write hit the
    same key and was dropped, so the next package told the Planner what the reviewer
    had said and not what was wrong with its own last answer.  The repair record is a
    different fact about a different thing, so it gets its own key.
    """

    world, evidence = blocked
    config = OrchestratorConfig(
        evidence_root=evidence,
        max_concurrency=1,
        test_timeout_seconds=5,
        max_root_review_repairs=1,
        max_planning_attempts=4,
    )

    async def case() -> list[dict[str, Any]]:
        async with Orchestrator(config, RoleScriptedProvider({"planner": []})) as loop:
            world.env.semantics = HtnStore(loop.store)
            loop.install_hierarchical(planning=world.env)
            mission = loop.store.get_mission(world.mission.id)
            assert await loop._advance_root_review(mission, loop._new_mode(mission))
            intent = next(
                item
                for item in loop.store.list_intents(
                    "PENDING", "CLAIMED", "AGENT_CREATED", "SUBMITTED"
                )
                if item.mission_id == world.mission.id and item.kind == "plan"
            )
            await loop._planning_rejected(
                intent, reason="proposal_not_grounded", detail={"error": "named a stranger"}
            )
            return [
                dict(item.payload)
                for item in loop.store.list_events(world.mission.id)
                if item.type == "PlanningRejected"
            ]

    reasons = [str(item["reason"]) for item in asyncio.run(case())]
    assert ROOT_REVIEW_REPAIR_REASON in reasons, "why the round was opened"
    assert "proposal_not_grounded" in reasons, "and what came back from it"


def test_the_rung_after_a_refused_repair_round_cannot_crash_the_loop_either(tmp_path) -> None:
    """Verification P0-1 residual: the fix had only closed the *opening* of the round.

    ``_repair_after_root_review`` opens one round.  When the Planner's answer to it is
    refused, ``_planning_rejected`` climbs the ordinary ladder — with the runner passing
    ``max_planning_attempts=3`` that is not an edge case, it is the next thing that
    happens — and that rung was still a bare ``_try_planner_intent``.  So the same
    ``BudgetExhausted`` escaped ``_cycle()`` one rung later, with the Mission left ACTIVE
    and no ``MissionFailed``: exactly the crash P0-1 was about, postponed.

    The account here holds one reservation and a hundred tokens over: the repair round
    takes the reservation, and the rung after it cannot be funded.
    """

    world = _rejected_world(tmp_path, findings=BLOCKER, key="p23d-repair-rung", tokens=4100)
    evidence = Path(tmp_path) / "evidence"
    config = OrchestratorConfig(
        evidence_root=evidence,
        max_concurrency=1,
        test_timeout_seconds=5,
        max_root_review_repairs=1,
        max_planning_attempts=3,
    )

    async def case() -> dict[str, Any]:
        async with Orchestrator(config, RoleScriptedProvider({"planner": []})) as loop:
            world.env.semantics = HtnStore(loop.store)
            loop.install_hierarchical(planning=world.env)
            mission = loop.store.get_mission(world.mission.id)
            assert await loop._advance_root_review(mission, loop._new_mode(mission))
            assert loop.store.get_mission(world.mission.id).status is MissionStatus.ACTIVE
            intent = next(
                item
                for item in loop.store.list_intents(
                    "PENDING", "CLAIMED", "AGENT_CREATED", "SUBMITTED"
                )
                if item.mission_id == world.mission.id and item.kind == "plan"
            )
            # What ``_collect_plan`` does with an unreadable repair proposal.
            await loop._planning_rejected(
                intent, reason="proposal_unreadable", detail={"error": "junk"}
            )
            final = loop.store.get_mission(world.mission.id)
            return {
                "status": final.status,
                "stop_reason": final.stop_reason,
                "report": dict(final.final_report or {}),
                "events": [item.type for item in loop.store.list_events(world.mission.id)],
            }

    outcome = asyncio.run(case())
    assert outcome["status"] is MissionStatus.FAILED
    assert outcome["stop_reason"] == str(MissionStopReason.BUDGET_EXHAUSTED)
    assert "MissionFailed" in outcome["events"]
    assert "planning_failure" not in outcome["report"], (
        "a Mission that holds a committed plan did not fail *at planning*"
    )


# ======================================================================================
# verification P2-A / P2-F: the two routing paths, on a Mission that already has a plan
# ======================================================================================


def _repair_with_broken_planner(
    world: World, evidence: Path, error: Exception, *, wait_seconds: float = 300.0
) -> dict[str, Any]:
    """Open D5-A's repair round with ``_create_planner_intent`` raising ``error``."""

    config = OrchestratorConfig(
        evidence_root=evidence,
        max_concurrency=1,
        test_timeout_seconds=5,
        max_root_review_repairs=1,
        max_planning_attempts=4,
        profile_wait_seconds=wait_seconds,
    )

    async def case() -> dict[str, Any]:
        async with Orchestrator(config, RoleScriptedProvider({"planner": []})) as loop:
            world.env.semantics = HtnStore(loop.store)
            loop.install_hierarchical(planning=world.env)

            async def refuse(*_args: Any, **_kwargs: Any) -> Any:
                raise error

            loop._create_planner_intent = refuse  # type: ignore[method-assign]
            mission = loop.store.get_mission(world.mission.id)
            moved = await loop._advance_root_review(mission, loop._new_mode(mission))
            deferred = dict(loop._deferred_planning)
            loop._prune_deferred()
            after_prune = dict(loop._deferred_planning)
            final = loop.store.get_mission(world.mission.id)
            return {
                "moved": moved,
                "deferred": deferred,
                "after_prune": after_prune,
                "status": final.status,
                "stop_reason": final.stop_reason,
                "report": dict(final.final_report or {}),
                "events": [item.type for item in loop.store.list_events(world.mission.id)],
            }

    return asyncio.run(case())


def test_a_repair_round_whose_pool_is_cooling_down_keeps_its_place_in_the_queue(
    tmp_path,
) -> None:
    """Verification P2-A (mutation VH survived): ``_prune_deferred``'s criterion.

    It used to drop every Mission that was not PLANNING, which was right while the only
    deferred round was the one producing the first plan.  D5-A's round belongs to an
    ACTIVE Mission, so the prune threw it away — with ``max_root_review_repairs``
    already spent on it, meaning the Mission silently lost its one repair to a pool that
    was merely cooling down.
    """

    from agent_orchestrator.runtime.model_router import RoutingUnavailable

    world = _rejected_world(tmp_path, findings=BLOCKER, key="p23d-repair-cooldown")
    outcome = _repair_with_broken_planner(
        world, Path(tmp_path) / "evidence", RoutingUnavailable("default", None)
    )
    assert outcome["moved"] is False, "nothing was dispatched this cycle"
    assert outcome["deferred"], "the round is waiting, not lost"
    assert outcome["after_prune"] == outcome["deferred"], (
        "an ACTIVE Mission's deferred round survives the prune"
    )
    assert outcome["status"] is MissionStatus.ACTIVE, "waiting is not failing"


def test_a_deferred_repair_round_is_retried_and_its_exhaustion_is_still_caught(
    tmp_path,
) -> None:
    """Verification P2-A (mutation VI survived): the retry branch, and its guard.

    ``_retry_deferred_planning`` takes the waiting round up again; for a Mission past
    PLANNING it has to go through ``_planner_round_on_committed_plan``, or the retry is
    one more place ``BudgetExhausted`` leaves the loop.
    """

    from agent_orchestrator.runtime.model_router import RoutingUnavailable

    world = _rejected_world(
        tmp_path, findings=BLOCKER, key="p23d-repair-retry", tokens=4100
    )
    evidence = Path(tmp_path) / "evidence"
    config = OrchestratorConfig(
        evidence_root=evidence,
        max_concurrency=1,
        test_timeout_seconds=5,
        max_root_review_repairs=1,
        max_planning_attempts=4,
    )

    async def case() -> dict[str, Any]:
        async with Orchestrator(config, RoleScriptedProvider({"planner": []})) as loop:
            world.env.semantics = HtnStore(loop.store)
            loop.install_hierarchical(planning=world.env)
            original = loop._create_planner_intent

            async def refuse(*_args: Any, **_kwargs: Any) -> Any:
                raise RoutingUnavailable("default", None)

            loop._create_planner_intent = refuse  # type: ignore[method-assign]
            mission = loop.store.get_mission(world.mission.id)
            await loop._advance_root_review(mission, loop._new_mode(mission))
            assert loop._deferred_planning, "the round is waiting"
            # The pool comes back, and the account has meanwhile gone: the retry has to
            # end the Mission rather than the process.
            loop._create_planner_intent = original  # type: ignore[method-assign]
            loop.commit.ledger.reserve(
                account_id=mission_account(world.mission.id),
                subject_id="drain-the-account",
                tokens=4000,
                cost_micros=0,
                counts_attempt=False,
                mission_id=world.mission.id,
            )
            await loop._retry_deferred_planning()
            final = loop.store.get_mission(world.mission.id)
            return {
                "status": final.status,
                "stop_reason": final.stop_reason,
                "report": dict(final.final_report or {}),
            }

    outcome = asyncio.run(case())
    assert outcome["status"] is MissionStatus.FAILED
    assert outcome["stop_reason"] == str(MissionStopReason.BUDGET_EXHAUSTED)
    assert outcome["report"].get("detail", {}).get("phase") == "deferred_planning"
    assert "planning_failure" not in outcome["report"]


def test_a_repair_round_whose_package_is_refused_stops_the_mission_not_the_planning(
    tmp_path,
) -> None:
    """``ContextRejected`` on a Mission that already holds a plan (verification P2-A).

    And the payload it writes: ``ordinal`` is added here, where the round is one only
    P2.3d opens, and **not** on the PLANNING path whose ``MissionFailed.detail`` is a
    shipped shape (mutation VO / P2-F).
    """

    from agent_orchestrator.context.context_builder import ContextRejected

    world = _rejected_world(tmp_path, findings=BLOCKER, key="p23d-repair-context")
    outcome = _repair_with_broken_planner(
        world, Path(tmp_path) / "evidence", ContextRejected("package too large")
    )
    assert outcome["moved"] is False
    assert outcome["status"] is MissionStatus.FAILED
    assert outcome["stop_reason"] == str(MissionStopReason.CONTEXT_REJECTED)
    assert "planning_failure" not in outcome["report"], "it did not fail at planning"
    assert "TaskCancelled" in outcome["events"]
    assert outcome["report"]["detail"]["ordinal"] == 1
