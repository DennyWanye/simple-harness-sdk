# SPDX-License-Identifier: Apache-2.0
"""An Assurance-lane planning round with an unknown Provider outcome is bounded.

host-final-arp10 (2026-09-24): a Planner turn of an assured Mission lost its stream after
hand-off; the lane recorded AssuranceProviderReconciliationRequired and nothing acted until
the react wall clock ~17 minutes later.  Independent adjudication: on this lane nothing is
ever re-handed off (that re-sends the original request) and reviews keep their original
executor (§6.2), but a Planner / MethodSynthesizer round is not a review — after the same
bound as P2.3f it ends through its own failure door, which on this lane keeps the UNKNOWN
grants and the reservation.  Reviews and Worker attempts keep waiting (unchanged).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from _deploy import deployment, spec

from agent_orchestrator.orchestrator import assurance_review_wait
from agent_orchestrator.orchestrator.event_handler import Orchestrator
from agent_orchestrator.runtime.agent_worker import Liveness
from agent_orchestrator.storage.assurance_store import AssuranceStore

BLOCKED = Liveness(exists=True, state="waiting", blocked=True, blocker={"kind": "provider"}, progress=1, settled=False)


def _intent(mission_id: str, *, kind: str, role: str, review: bool = False):  # type: ignore[no-untyped-def]
    config = {"role": role}
    if review:
        config["assurance_protocol"] = "assurance-exec-v1.1"
    return SimpleNamespace(intent_id=f"intent-{kind}-{role}", replays=0, kind=kind, config=config,
                           mission_id=mission_id, subject_id=f"{mission_id}:{role}:1", agent_id="agent-x",
                           expected_turn_id="agent-x:input:1")


@pytest.mark.parametrize(
    ("kind", "role", "review", "ends"),
    [
        ("plan", "planner", False, True),
        ("plan", "method_synthesizer", False, True),
        ("plan", "root_reviewer", False, False),
        ("plan", "planner", True, False),  # an assured review of any purpose
        ("critic", "critic", True, False),
        ("attempt", "worker", False, False),
    ],
)
def test_only_assured_planning_rounds_end_after_the_bound(tmp_path, monkeypatch, kind, role, review, ends) -> None:
    async def case() -> None:
        async with deployment(tmp_path) as world:
            orch = world.orch
            mission, _ = world.commit.create_mission(spec("assured-unknown"))
            assert AssuranceStore(world.store).lane(mission.id) == "ASSURANCE_1_1"
            waits: list = []
            doors: list = []
            monkeypatch.setattr(assurance_review_wait, "record_provider_wait", lambda _c, intent, _l: waits.append(intent.intent_id))
            monkeypatch.setattr(Orchestrator, "_service_blocker_limit", property(lambda self: 0.05))
            monkeypatch.setattr(orch, "_new_mode", lambda m: object())

            async def door(intent, mission, new_mode, *, detail):  # type: ignore[no-untyped-def]
                doors.append(dict(detail))

            monkeypatch.setattr(orch, "_give_up_blocked_plan_intent", door)
            rehandoffs: list = []
            monkeypatch.setattr(orch.commit, "rehandoff_service_intent", lambda *a, **k: rehandoffs.append(a))
            intent = _intent(mission.id, kind=kind, role=role, review=review)

            assert await orch._resolve_provider_blocked_service(intent, BLOCKED) == "give_up"
            assert doors == []  # the bound has not passed yet
            await asyncio.sleep(0.08)
            assert await orch._resolve_provider_blocked_service(intent, BLOCKED) == "give_up"
            assert rehandoffs == []  # never re-sent on this lane
            assert len(waits) == 2  # the wait is recorded (idempotent in production)
            if ends:
                assert len(doors) == 1 and doors[0]["rehandoffs"] == 0 and doors[0]["assurance_lane"] is True
                assert doors[0]["waited_seconds"] >= 0.05
            else:
                assert doors == []

    asyncio.run(case())
