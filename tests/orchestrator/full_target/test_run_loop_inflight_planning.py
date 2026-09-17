# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""P2.3e: ``run()`` must not leave while a planning-class intent is still in flight.

The Grok acceptance rerun (H-L3-C1, three identical episodes on 05cfbb83) found it.
The runner's ``first_evidence_round`` had already recorded ``code.test-is-failing``
twice from the same observer, so D2b's saturation held on the **first** planning
cycle: a Planner round and a MethodSynthesizer round were created together and both
``InputSubmitted``.  About 28.5 s later ``orchestrator.run()`` returned, the
``Orchestrator`` context closed, and both agent runs were cancelled the moment they
finally got to start — ``runtime_boundary_interrupted`` on the synthesiser,
``provider_error_after_handoff`` on the Planner — with the Mission left at PLANNING,
no ``IntentSettled``, no ``PlanningRejected``, no ``MissionFailed``.

The cause was not a limit and not the proxy: ``_request_method_synthesis`` ran on
every cycle, ``create_service_intent`` is idempotent per subject and simply handed the
existing intent back, and the method reported that as **progress**.  A progressing
cycle neither sleeps nor yields, so ``run()`` spun through its ``max_cycles`` budget
(10 000 cycles in ~28 s, one cycle per 2.85 ms) with the runtime's turn tasks starved
the whole time, and then left by the ``max_cycles`` exit with two turns submitted and
nobody left to collect them.

Two things are asserted here, each with its own test:

* ``run()`` stays until a submitted planning intent is collected — however long the
  model takes (a held gate stands in for the sixty-second Grok call);
* two planning-class intents out at once each reach their own settlement, with the
  round's events written in full, when the loop is left to run on its own.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

_HTN_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "htn"
if str(_HTN_FIXTURES) not in sys.path:
    sys.path.insert(0, str(_HTN_FIXTURES))

import test_evidence_saturation as saturation  # noqa: E402

from agent_orchestrator.contracts.models import MissionStatus  # noqa: E402
from agent_orchestrator.orchestrator.event_handler import Orchestrator  # noqa: E402
from agent_orchestrator.runtime.assembly import OrchestratorConfig  # noqa: E402
from agent_orchestrator.storage.htn_store import HtnStore  # noqa: E402
from agent_orchestrator.testing.fixtures import (  # noqa: E402
    RoleScriptedProvider,
    method_proposal_step,
)

OPEN_INTENT_STATES = ("PENDING", "CLAIMED", "AGENT_CREATED", "SUBMITTED")


def _saturated_world(tmp_path, *, key: str):
    """H-L3-C1's opening position: one gated method, two readings already recorded.

    The observations are written **before** the Orchestrator opens, exactly as the
    acceptance runner's ``first_evidence_round`` writes them before ``run()``.
    """

    evidence = Path(tmp_path) / "evidence"
    evidence.mkdir(parents=True, exist_ok=True)
    world = saturation._gated_world(evidence, key=key)
    for ordinal in (1, 2):
        saturation._observe(world, observer="plan.observer", ordinal=ordinal)
    invented = saturation._free_method()
    world.store.close()
    config = OrchestratorConfig(
        evidence_root=evidence,
        # The runner's value: with one slot the synthesiser's reservation defers the
        # Planner instead of racing it, and the racing shape is the one under test.
        max_concurrency=3,
        test_timeout_seconds=60,
        max_planning_attempts=2,
    )
    return world, invented, config


def _provider(invented, *, gate: asyncio.Event | None) -> RoleScriptedProvider:
    return RoleScriptedProvider(
        {
            "planner": [
                "no plan is possible with the methods on offer",
                saturation._adopt(invented.method_ref()),
            ],
            "method_synthesizer": [method_proposal_step(invented.to_json())],
        },
        gate=gate,
    )


def _open_plan_intents(loop: Orchestrator, mission_id: str) -> list[str]:
    return sorted(
        item.subject_id
        for item in loop.store.list_intents(*OPEN_INTENT_STATES)
        if item.kind == "plan" and item.mission_id == mission_id
    )


def _subject_of(loop: Orchestrator, intent_id: str) -> str:
    intent = loop.store.get_intent(intent_id)
    return "" if intent is None else str(intent.subject_id)


def _snapshot(loop: Orchestrator, mission_id: str) -> dict[str, Any]:
    events = list(loop.store.list_events(mission_id))
    mission = loop.store.get_mission(mission_id)
    return {
        "status": None if mission is None else mission.status,
        "types": [item.type for item in events],
        "settled": sorted(
            _subject_of(loop, str(item.payload.get("intent_id", "")))
            for item in events
            if item.type == "IntentSettled"
        ),
        "open": _open_plan_intents(loop, mission_id),
        "committed": [
            item.payload for item in events if item.type == "PlanRevisionCommitted"
        ],
        "synthesis": [
            item.payload for item in events if item.type == "MethodSynthesisRoundRecorded"
        ],
    }


def test_run_waits_for_two_inflight_planning_intents_however_slow_the_model_is(
    tmp_path,
) -> None:
    """The red test for the run-exit defect.

    The provider holds every call at a gate, so both rounds are "a slow real model
    turn" for as long as the test says.  ``max_cycles`` is small on purpose: on the
    defective loop every cycle is a progressing one, so the budget is spent in
    milliseconds and ``run()`` returns with both intents still SUBMITTED.  On a loop
    that waits, the budget bounds *work* and not waiting — the docstring's own
    promise — and ``run()`` is still there when the gate opens.
    """

    world, invented, config = _saturated_world(tmp_path, key="p23e-run-exit-held")
    gate = asyncio.Event()
    provider = _provider(invented, gate=gate)

    async def case() -> dict[str, Any]:
        async with Orchestrator(config, provider, poll_interval=0.02) as loop:
            world.env.semantics = HtnStore(loop.store)
            loop.install_hierarchical(planning=world.env)
            mission_id = world.mission.id
            # The fixture has already moved the Mission to PLANNING (its ``begin_planning``
            # is the real loop's), so the round ``_start_planning`` would open on a
            # CREATED Mission is opened here by the same call; the synthesis round is
            # the loop's own first cycle.
            await loop._try_planner_intent(mission_id, ordinal=1)
            running = asyncio.create_task(loop.run(max_cycles=120))
            # Long enough for a busy loop to burn 120 cycles many times over, short
            # enough that a waiting loop has only polled.
            await asyncio.sleep(1.0)
            held = {
                "returned": running.done(),
                "open": _open_plan_intents(loop, mission_id),
                "calls_started": dict(provider.by_role),
                **{f"held_{key}": value for key, value in _snapshot(loop, mission_id).items()},
            }
            gate.set()
            await asyncio.wait_for(running, timeout=30)
            return {"held": held, **_snapshot(loop, mission_id)}

    outcome = asyncio.run(case())
    held = outcome["held"]
    assert held["open"] == [
        f"{world.mission.id}:planner:1",
        f"{world.mission.id}:synthesizer:{saturation.ROOT_TASK}:1",
    ], f"the racing shape was not reproduced: {held}"
    assert held["returned"] is False, (
        "run() returned while both planning intents were still in flight: "
        f"{held}"
    )
    # A waiting loop yields; the runtime's turn tasks reach the provider and hold at
    # the gate — a starved loop never lets them start (the 28.5 s of the episode).
    assert held["calls_started"].get("planner") == 1, held
    assert held["calls_started"].get("method_synthesizer") == 1, held
    assert outcome["open"] == [], f"an intent was left in flight: {outcome}"
    assert outcome["synthesis"] and outcome["synthesis"][0]["admitted"] is True, outcome
    assert outcome["committed"], outcome["types"]
    assert outcome["status"] is not MissionStatus.PLANNING, outcome["status"]


def test_two_concurrent_planning_intents_each_settle_with_their_events(tmp_path) -> None:
    """End to end on ``run()`` alone: no ``_cycle`` stepping, no gate, no hand-settling.

    Planner #1 and the synthesiser are out together; #1 is refused, the synthesised
    method is admitted, Planner #2 adopts it.  Every one of those rounds has its own
    ``IntentSettled``, and the loop got there in a handful of progressing cycles, not
    a spin — the P2.3d tests drove ``_cycle`` by hand precisely because ``run()`` could
    not be trusted with an unfinished turn, and that is the thing that is fixed.
    """

    world, invented, config = _saturated_world(tmp_path, key="p23e-run-exit-e2e")
    provider = _provider(invented, gate=None)

    async def case() -> dict[str, Any]:
        async with Orchestrator(config, provider, poll_interval=0.02) as loop:
            world.env.semantics = HtnStore(loop.store)
            loop.install_hierarchical(planning=world.env)
            mission_id = world.mission.id
            await loop._try_planner_intent(mission_id, ordinal=1)
            progressing = 0
            inner = loop._cycle_inner

            async def counted() -> bool:
                nonlocal progressing
                result = await inner()
                progressing += int(result)
                return result

            loop._cycle_inner = counted  # type: ignore[method-assign]
            await asyncio.wait_for(loop.run(max_cycles=400), timeout=60)
            return {"progressing": progressing, **_snapshot(loop, mission_id)}

    outcome = asyncio.run(case())
    root = saturation.ROOT_TASK
    expected = {
        f"{world.mission.id}:planner:1",
        f"{world.mission.id}:planner:2",
        f"{world.mission.id}:synthesizer:{root}:1",
    }
    assert set(outcome["settled"]) >= expected, (
        f"a planning-class intent never reached its settlement: {outcome}"
    )
    assert outcome["open"] == [], outcome
    assert "PlanningRejected" in outcome["types"], outcome["types"]
    assert outcome["synthesis"] and outcome["synthesis"][0]["admitted"] is True, outcome
    assert outcome["committed"], outcome["types"]
    assert outcome["status"] is not MissionStatus.PLANNING, outcome["status"]
    assert outcome["progressing"] < 60, (
        f"the loop spun {outcome['progressing']} progressing cycles for three rounds"
    )
