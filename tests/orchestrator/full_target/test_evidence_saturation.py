# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""P2.3d / defect D2b: "look again" stops being an answer once the looking is settled.

``SYNTHESIS_WORTHY_REFUSALS`` deliberately leaves ``NEEDS_EVIDENCE`` out — the repair
for an unknown precondition is to observe, not to invent a method.  That is right
until observing cannot change anything, and the Grok acceptance run found the case
where it cannot.

``code.test-is-failing`` is an **OPEN** predicate.  The observer ran ``pytest tests``,
found the suite green, and recorded ``polarity=false`` with ``coverage=BEST_EFFORT``.
§6.6: a non-authoritative negative in an open world contributes ``NO_SUPPORT``, so
``atom_truth`` answers UNKNOWN — not FALSE.  Every method for the goal was therefore
``NEEDS_EVIDENCE``, and the two repairs cancelled each other out every cycle:

* ``_gather_evidence`` recorded another observation and reported progress;
* ``goals_needing_method`` returned nothing, because NEEDS_EVIDENCE is not
  synthesis-worthy;
* the truth stayed UNKNOWN, because the observation was the same one again.

All six L3 episodes burned their planning ladder in that live-lock.  The narrow fix is
below: a precondition one observer has already read ``evidence_saturation_rounds``
times, still unknown, counts as a refusal a *different method* could route around.

What is deliberately **not** widened: I18.  Nothing here turns UNKNOWN into TRUE or
opens a safety gate — it only decides whether proposing a new method is a sensible
next question.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

_HTN_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "htn"
if str(_HTN_FIXTURES) not in sys.path:
    sys.path.insert(0, str(_HTN_FIXTURES))

import test_htn_end_to_end as e2e  # noqa: E402
from htn_world import atom, method, param, step  # noqa: E402
from test_htn_end_to_end import ROOT_TASK, World, build_world  # noqa: E402

from agent_orchestrator.contracts.evidence_state import (  # noqa: E402
    ObservationRecord,
    QueryCompleteness,
)
from agent_orchestrator.contracts.htn import TaskForm  # noqa: E402
from agent_orchestrator.contracts.models import ContractError, MissionStatus  # noqa: E402
from agent_orchestrator.contracts.semantic_base import (  # noqa: E402
    TypedRef,
    TypedRefKind,
    content_hash_of,
)
from agent_orchestrator.contracts.state_machines import TERMINAL_MISSION  # noqa: E402
from agent_orchestrator.orchestrator.event_handler import Orchestrator  # noqa: E402
from agent_orchestrator.orchestrator.hierarchical_dispatch import (  # noqa: E402
    DEFAULT_EVIDENCE_SATURATION_ROUNDS,
    HierarchicalDispatch,
)
from agent_orchestrator.planning.htn.applicability import ApplicabilityStatus  # noqa: E402
from agent_orchestrator.runtime.assembly import OrchestratorConfig  # noqa: E402
from agent_orchestrator.storage.htn_store import HtnStore  # noqa: E402
from agent_orchestrator.testing.fixtures import RoleScriptedProvider  # noqa: E402

PREDICATE = "plan.ready"


def _gated(method_id: str = "plan.outer"):
    """``_outer``'s shape, but every method for the root goal is precondition-gated.

    That is the L3 situation: ``code.fix-by-patch``, ``code.fix-by-revert`` and
    ``code.fix-by-assessed-revert`` were **all** NEEDS_EVIDENCE, so no method applied
    and no method could be synthesised either.
    """

    return method(
        method_id,
        "plan.goal",
        parameter_schema="plan.goal.params",
        applicable=(atom(PREDICATE, {"subject": param("subject")}),),
        steps=(
            step(
                "leaf",
                "plan.leaf",
                TaskForm.PRIMITIVE,
                {"subject": param("subject")},
                capabilities=("plan.read",),
            ),
        ),
        links=(("c-root", "leaf", "c-done"),),
        finalizer="leaf",
    )


def _gated_world(tmp_path, *, key: str) -> World:
    """A Mission whose only registered method is refused for an unknown precondition."""

    original_env, original_outer = e2e._env, e2e._outer

    def env_with_predicate(mission: str):
        env = original_env(mission)
        # OPEN, and with a named observer: the combination the L3 live-lock is made of.
        env.register_predicate(PREDICATE, closed=False, observers=("plan.observer",))
        return env

    e2e._env, e2e._outer = env_with_predicate, _gated
    try:
        return build_world(tmp_path, key=key)
    finally:
        e2e._env, e2e._outer = original_env, original_outer


@pytest.fixture
def gated(tmp_path) -> World:
    return _gated_world(tmp_path, key="p23d-saturation")


def _key(world: World) -> str:
    from htn_world import ref

    from agent_orchestrator.knowledge.predicates import proposition_key

    return proposition_key(world.env.predicates.require(ref(PREDICATE)), {"subject": "alpha"})


def _observe(world: World, *, observer: str, ordinal: int) -> None:
    """One honest reading that does not settle the proposition (``polarity=false``)."""

    key = _key(world)
    HtnStore(world.store).insert_observation(
        world.mission.id,
        ObservationRecord(
            observation_id=f"obs-{observer}-{ordinal}",
            proposition_key=key,
            polarity=False,
            source_ref=TypedRef(
                kind=TypedRefKind.OBSERVATION,
                id=f"src-{observer}-{ordinal}",
                revision=1,
                content_hash=content_hash_of(f"{observer}:{ordinal}"),
            ),
            observed_at_ms=1_000 * ordinal,
            recorded_at_ms=1_000 * ordinal,
            coverage=QueryCompleteness.BEST_EFFORT,
            observer_id=observer,
        ),
    )


def _report(world: World):
    entries = world.dispatch.method_applicability(world.mission.id)
    assert entries, "the fixture's method really is refused"
    report = entries[0].report
    assert report.status is ApplicabilityStatus.NEEDS_EVIDENCE
    return report


# ======================================================================================
# 1. the live-lock, and the way out of it
# ======================================================================================


def test_an_unobserved_precondition_is_not_saturated(gated: World) -> None:
    """Nobody has looked yet, so looking is exactly the right repair."""

    assert world_needs(gated) == ()
    assert gated.dispatch._evidence_is_saturated(gated.mission.id, _report(gated)) is False


def test_one_reading_is_not_saturation(gated: World) -> None:
    """A first answer that did not settle it may still be a transient."""

    _observe(gated, observer="plan.observer", ordinal=1)
    assert world_needs(gated) == ()


def test_the_same_observer_reading_twice_with_no_change_is_saturation(gated: World) -> None:
    """The six L3 episodes, released.

    **Mutation**: drop ``_evidence_is_saturated`` from the ``any(...)`` in
    ``goals_needing_method`` and this goes red while every other test in this file
    stays green — which is the whole asymmetry the widening is.
    """

    for ordinal in (1, 2):
        _observe(gated, observer="plan.observer", ordinal=ordinal)
    assert gated.dispatch._evidence_is_saturated(gated.mission.id, _report(gated)) is True
    assert world_needs(gated) == (ROOT_TASK,)


def test_two_different_observers_once_each_is_not_saturation(gated: World) -> None:
    """A second observer is a genuinely new look, not a repeat of the first one."""

    _observe(gated, observer="plan.observer", ordinal=1)
    _observe(gated, observer="plan.other-observer", ordinal=2)
    assert world_needs(gated) == ()


def test_the_bound_is_configuration(tmp_path) -> None:
    world = _gated_world(tmp_path, key="p23d-saturation-n")
    world.dispatch = HierarchicalDispatch(
        world.store, world.service, planning=world.env, evidence_saturation_rounds=3
    )
    for ordinal in (1, 2):
        _observe(world, observer="plan.observer", ordinal=ordinal)
    assert world_needs(world) == ()
    _observe(world, observer="plan.observer", ordinal=3)
    assert world_needs(world) == (ROOT_TASK,)


def test_the_default_is_two_and_a_bound_below_one_is_refused(tmp_path) -> None:
    assert DEFAULT_EVIDENCE_SATURATION_ROUNDS == 2
    world = build_world(tmp_path, key="p23d-saturation-cfg")
    with pytest.raises(ContractError, match="evidence_saturation_rounds"):
        HierarchicalDispatch(
            world.store, world.service, planning=world.env, evidence_saturation_rounds=0
        )


# ======================================================================================
# 2. the line that is not moved
# ======================================================================================


def test_saturation_never_settles_the_proposition(gated: World) -> None:
    """I18 is untouched: the truth is still UNKNOWN and the method still does not apply.

    Saturation answers "is another method worth proposing", never "is the precondition
    satisfied".  A safety gate reading this proposition sees exactly what it saw before.
    """

    for ordinal in (1, 2):
        _observe(gated, observer="plan.observer", ordinal=ordinal)
    report = _report(gated)
    assert report.status is ApplicabilityStatus.NEEDS_EVIDENCE
    assert not report.applicable


def world_needs(world: World) -> tuple[str, ...]:
    return world.dispatch.goals_needing_method(world.mission.id)


# ======================================================================================
# review P1-1: the other half — a synthesised method has to reach the Planner
# ======================================================================================


def _free_method():
    """The method the synthesiser invents: same goal, no precondition to be unsure of."""

    return method(
        "plan.outer.free",
        "plan.goal",
        parameter_schema="plan.goal.params",
        applicable=(),
        steps=(
            step(
                "leaf",
                "plan.leaf",
                TaskForm.PRIMITIVE,
                {"subject": param("subject")},
                capabilities=("plan.read",),
            ),
        ),
        links=(("c-root", "leaf", "c-done"),),
        finalizer="leaf",
    )


def _adopt(reference) -> str:
    from test_htn_end_to_end import ROOT_DUTY  # noqa: PLC0415

    from agent_orchestrator.testing.fixtures import plan_revision_proposal_step

    return plan_revision_proposal_step(
        proposal_id="prop-synthesised",
        expected_plan_revision=0,
        read_set=[
            {
                "kind": "method",
                "id": reference.method_id,
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
                    "id": reference.method_id,
                    "version": reference.version,
                    "content_hash": reference.content_hash,
                },
                "bindings": {},
            }
        ],
    )


def test_a_saturated_goal_is_synthesised_and_the_new_method_is_planned(tmp_path) -> None:
    """H-L3-C1's shape, driven end to end on a real ``Orchestrator``.

    Review P1-1: D2b was only half of the way out of the live-lock.  ``goals_needing_method``
    started returning the goal and a MethodSynthesizer round opened, but nothing on the
    other side of that round asked the Planner anything:

    * ``_collect_synthesizer`` recorded the outcome and settled the intent, full stop —
      all five callers of ``_try_planner_intent`` were elsewhere;
    * the admitted method went into the in-memory registry and **not** into the library,
      so a Planner that did name it compiled into "method … is not stored";
    * ``_planning_rejected`` opened the next rung the moment the previous one was
      refused, so the ladder was spent — and the Mission failed — while the synthesiser
      was still being asked.  Whether a Mission survived came down to which of two model
      calls answered first.

    All three are fixed here, and this test is the one that can tell: the only method the
    Mission starts with is precondition-gated on an OPEN predicate that two readings have
    left UNKNOWN, so nothing can be planned until the synthesised method arrives.
    """

    from agent_orchestrator.testing.fixtures import method_proposal_step

    evidence = Path(tmp_path) / "evidence"
    evidence.mkdir(parents=True, exist_ok=True)
    world = _gated_world(evidence, key="p23d-saturation-e2e")
    for ordinal in (1, 2):
        _observe(world, observer="plan.observer", ordinal=ordinal)
    invented = _free_method()
    world.store.close()

    config = OrchestratorConfig(
        evidence_root=evidence,
        max_concurrency=1,
        test_timeout_seconds=60,
        max_planning_attempts=2,
    )
    provider = RoleScriptedProvider(
        {
            # Two rounds that cannot work: every method the Mission holds is refused for
            # an unknown precondition, so the honest Planner has nothing to propose.
            "planner": [
                "no plan is possible with the methods on offer",
                "still nothing; the only method is gated on an unknown precondition",
                _adopt(invented.method_ref()),
            ],
            "method_synthesizer": [method_proposal_step(invented.to_json())],
        }
    )

    async def case() -> dict[str, Any]:
        async with Orchestrator(config, provider) as loop:
            world.env.semantics = HtnStore(loop.store)
            loop.install_hierarchical(planning=world.env)
            # ``run()`` is driven a cycle at a time rather than left to its own poll:
            # ``_observe_liveness`` reports a heartbeat as progress, so an unfinished
            # scripted turn spends ``max_cycles`` before it ever gets to answer.  The
            # body is the real one — this only controls when it is entered.
            for _ in range(24):
                await loop._cycle()
                await asyncio.sleep(0.02)
                mission = loop.store.get_mission(world.mission.id)
                if mission is not None and mission.status in TERMINAL_MISSION:
                    break
            events = list(loop.store.list_events(world.mission.id))
            return {
                "types": [item.type for item in events],
                "synthesis": [
                    item.payload
                    for item in events
                    if item.type == "MethodSynthesisRoundRecorded"
                ],
                "committed": [
                    item.payload
                    for item in events
                    if item.type == "PlanRevisionCommitted"
                ],
                "planner_rounds": provider.by_role.get("planner", 0),
                "status": loop.store.get_mission(world.mission.id).status,
                "progress": list(loop.progress_log),
                "roles": dict(provider.by_role),
            }

    outcome = asyncio.run(case())
    assert outcome["synthesis"] and outcome["synthesis"][0]["admitted"] is True, (
        f"the saturated goal never reached a synthesis round: {outcome['types']} "
        f"progress={outcome['progress']} roles={outcome['roles']}"
    )
    assert outcome["committed"], (
        "the synthesised method never became a plan — the round bought nothing: "
        f"{outcome['types']}"
    )
    assert outcome["planner_rounds"] == 3, "one more round than the ladder, not a loop"


def test_the_planning_ladder_waits_for_a_synthesis_round_instead_of_racing_it(tmp_path) -> None:
    """The third part of P1-1: which of two model calls answers first is not a design.

    ``_planning_rejected`` opened the next rung the instant the previous one was
    refused.  On the L3 shape that meant planner #2 was created *before* the
    synthesiser had answered, its package sealed on the old library; when it was refused
    too, ``ordinal >= max_planning_attempts`` and the Mission was failed — with a
    synthesis round still in flight that was about to hand it the method it needed.
    Raising ``max_planning_attempts`` does not fix that; it only moves the race.

    So: while a synthesis round is out, a spent ladder waits rather than ends, and an
    admitted method buys exactly one more rung.  The bound still holds — one synthesis
    round per goal (``synthesis_round_recorded``), one extra rung per admitted method.
    """

    from agent_orchestrator.testing.fixtures import method_proposal_step

    evidence = Path(tmp_path) / "evidence"
    evidence.mkdir(parents=True, exist_ok=True)
    world = _gated_world(evidence, key="p23d-saturation-race")
    for ordinal in (1, 2):
        _observe(world, observer="plan.observer", ordinal=ordinal)
    invented = _free_method()
    world.store.close()

    config = OrchestratorConfig(
        evidence_root=evidence,
        max_concurrency=1,
        test_timeout_seconds=60,
        max_planning_attempts=2,
    )
    provider = RoleScriptedProvider(
        {
            "planner": [
                "nothing to propose",
                "still nothing",
                _adopt(invented.method_ref()),
            ],
            "method_synthesizer": [method_proposal_step(invented.to_json())],
        }
    )

    async def case() -> dict[str, Any]:
        async with Orchestrator(config, provider) as loop:
            world.env.semantics = HtnStore(loop.store)
            loop.install_hierarchical(planning=world.env)
            mission = loop.store.get_mission(world.mission.id)
            # Both rounds are out at once, which is the racing shape.
            await loop._try_planner_intent(mission.id, ordinal=1)
            await loop._request_method_synthesis(mission)
            def planner_intent(ordinal: int):
                return next(
                    item
                    for item in loop.store.list_intents(
                        "PENDING", "CLAIMED", "AGENT_CREATED", "SUBMITTED"
                    )
                    if item.kind == "plan"
                    and str(item.config.get("role", "")) != "method_synthesizer"
                    and int(item.config.get("ordinal", 0)) == ordinal
                )

            # The Planner answers first, twice, and is refused both times: with
            # ``max_planning_attempts=2`` the ladder is spent while the synthesiser is
            # still out.  Before the fix this is where the Mission was failed.
            await loop._planning_rejected(
                planner_intent(1), reason="proposal_unreadable", detail={"error": "no block"}
            )
            await loop._planning_rejected(
                planner_intent(2), reason="proposal_unreadable", detail={"error": "again"}
            )
            waited = loop.store.get_mission(world.mission.id).status
            for _ in range(24):
                await loop._cycle()
                await asyncio.sleep(0.02)
                current = loop.store.get_mission(world.mission.id)
                if current is not None and current.status in TERMINAL_MISSION:
                    break
            events = list(loop.store.list_events(world.mission.id))
            return {
                "waited": waited,
                "types": [item.type for item in events],
                "committed": [
                    item.payload for item in events if item.type == "PlanRevisionCommitted"
                ],
            }

    outcome = asyncio.run(case())
    assert outcome["waited"] is not MissionStatus.FAILED, (
        "the Mission was ended on the old library while the new one was being written"
    )
    assert "MissionFailed" not in outcome["types"], outcome["types"]
    assert outcome["committed"], outcome["types"]


def test_a_refused_synthesis_round_ends_the_wait_it_caused(tmp_path) -> None:
    """Verification VN: the other exit from the branch that makes the ladder wait.

    ``_planning_rejected`` stops short of failing a PLANNING Mission while a synthesis
    round is in flight — otherwise the Mission is ended on the old library.  That wait
    has to be ended by whoever caused it: if the synthesised method is *refused*, the
    Mission has no Planner round out, no rung left and nothing to dispatch, and leaving
    it there turns a decided failure into an idle one that the stall path has to guess at.
    """

    from agent_orchestrator.testing.fixtures import method_proposal_step

    evidence = Path(tmp_path) / "evidence"
    evidence.mkdir(parents=True, exist_ok=True)
    world = _gated_world(evidence, key="p23d-saturation-refused")
    for ordinal in (1, 2):
        _observe(world, observer="plan.observer", ordinal=ordinal)
    world.store.close()

    config = OrchestratorConfig(
        evidence_root=evidence,
        max_concurrency=1,
        test_timeout_seconds=60,
        max_planning_attempts=2,
    )
    # Readable by the codec, refused by the admission protocol for something the
    # model cannot correct: a complete method whose only step needs a capability this
    # deployment has never declared (``UNKNOWN_CAPABILITY``).  P2.3g: a reply the
    # *codec* cannot read is no longer a refusal — it is asked once more with the
    # codec's problems attached.  P2.3i: so is a refusal for a correctable slip (an
    # operator the package never offered is one — ``UNKNOWN_OPERATOR``), which is why
    # this test no longer scripts an unknown operator; it scripts the refusal that
    # concludes a round on the spot (see ``test_synthesis_rejection_reask.py``).
    refused = method(
        "plan.nowhere",
        "plan.goal",
        parameter_schema="plan.goal.params",
        applicable=(),
        steps=(
            step(
                "leaf",
                "plan.leaf",
                TaskForm.PRIMITIVE,
                {"subject": param("subject")},
                capabilities=("plan.read", "plan.capability-nobody-declares"),
            ),
        ),
        links=(("c-root", "leaf", "c-done"),),
        finalizer="leaf",
    )
    provider = RoleScriptedProvider(
        {
            "planner": ["nothing to propose", "still nothing"],
            "method_synthesizer": [method_proposal_step(refused.to_json())],
        }
    )

    async def case() -> dict[str, Any]:
        async with Orchestrator(config, provider) as loop:
            world.env.semantics = HtnStore(loop.store)
            loop.install_hierarchical(planning=world.env)
            mission = loop.store.get_mission(world.mission.id)
            await loop._try_planner_intent(mission.id, ordinal=1)
            await loop._request_method_synthesis(mission)

            def planner_intent(ordinal: int):
                return next(
                    item
                    for item in loop.store.list_intents(
                        "PENDING", "CLAIMED", "AGENT_CREATED", "SUBMITTED"
                    )
                    if item.kind == "plan"
                    and str(item.config.get("role", "")) != "method_synthesizer"
                    and int(item.config.get("ordinal", 0)) == ordinal
                )

            await loop._planning_rejected(
                planner_intent(1), reason="proposal_unreadable", detail={"error": "no block"}
            )
            await loop._planning_rejected(
                planner_intent(2), reason="proposal_unreadable", detail={"error": "again"}
            )
            waiting = loop.store.get_mission(world.mission.id).status
            # ``_collect_plan`` settles the round it rejected; doing it by hand here is
            # what leaves the Mission in the state the wait is *about* — no Planner round
            # out, no rung left, one synthesis round still to answer.
            for item in loop.store.list_intents(
                "PENDING", "CLAIMED", "AGENT_CREATED", "SUBMITTED"
            ):
                if item.kind == "plan" and str(item.config.get("role", "")) != (
                    "method_synthesizer"
                ):
                    loop._settle_intent(item, "FAILED")
            for _ in range(24):
                await loop._cycle()
                await asyncio.sleep(0.02)
                current = loop.store.get_mission(world.mission.id)
                if current is not None and current.status in TERMINAL_MISSION:
                    break
            final = loop.store.get_mission(world.mission.id)
            return {
                "waiting": waiting,
                "status": final.status,
                "report": dict(final.final_report or {}),
                "synthesis": [
                    dict(item.payload)
                    for item in loop.store.list_events(world.mission.id)
                    if item.type == "MethodSynthesisRoundRecorded"
                ],
            }

    outcome = asyncio.run(case())
    assert outcome["waiting"] is MissionStatus.PLANNING, "the ladder waited, as it should"
    assert outcome["synthesis"] and outcome["synthesis"][0]["admitted"] is False
    assert outcome["synthesis"][0]["verdict"] == "REJECTED", outcome["synthesis"]
    assert outcome["synthesis"][0]["asks"] == 1, (
        "a refusal the model cannot correct is not re-asked (P2.3i)"
    )
    assert outcome["synthesis"][0]["problems"][0].startswith("UNKNOWN_CAPABILITY: ")
    assert outcome["status"] is MissionStatus.FAILED, "the wait ended, and it ended honestly"
    assert outcome["report"]["planning_failure"]["reason"] == "method_synthesis_refused"
