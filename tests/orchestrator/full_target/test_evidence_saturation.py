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

import sys
from pathlib import Path

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
from agent_orchestrator.contracts.models import ContractError  # noqa: E402
from agent_orchestrator.contracts.semantic_base import (  # noqa: E402
    TypedRef,
    TypedRefKind,
    content_hash_of,
)
from agent_orchestrator.orchestrator.hierarchical_dispatch import (  # noqa: E402
    DEFAULT_EVIDENCE_SATURATION_ROUNDS,
    HierarchicalDispatch,
)
from agent_orchestrator.planning.htn.applicability import ApplicabilityStatus  # noqa: E402
from agent_orchestrator.storage.htn_store import HtnStore  # noqa: E402

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
