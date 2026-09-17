# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""P2.3f: a service turn waiting on an unknown Provider outcome does not wait for ever.

Found by the P2.3e probe episode (H-L3-C1 on the Grok lane).  Planner round 2 was
handed off and the transport failed 0.2 s later.  The runtime did the right thing:
``ProviderTransportError`` is not a *definite* failure — the request may have reached
the model — so the invocation was settled ``UNKNOWN``, the run went to ``waiting``
with a ``provider`` wait blocker, and nothing was charged.  Then nothing happened.
``_observe_liveness`` looks only at ``liveness.exists`` for a ``plan`` intent, no
reconciliation port exists on that lane, and the intent stayed SUBMITTED — the
Mission at PLANNING, ``run()`` correctly still polling — until the runner's 1800 s
deadline.

This file pins the bounded answer:

* after ``min(stall_seconds, 300)`` seconds on the same blocker the request is handed
  off **once more** to a new executor (``ServiceIntentRehandedOff``; same subject,
  same reservation);
* a second unknown outcome ends the round through the role's own door — Planner:
  ``PlanningRejected{provider_outcome_unknown}`` and the ladder decides; Method-
  Synthesizer: ``MethodSynthesisRoundRecorded{UNANSWERED}`` and the synthesis wait
  ends; Critic: the runner's own "did not answer" path;
* the abandoned turn's charge stays unknown in the runtime ledger and only the
  executor that answered is imported;
* a legacy Mission is not touched.
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
import test_htn_end_to_end as e2e  # noqa: E402

from agent_orchestrator.contracts.models import MissionStatus  # noqa: E402
from agent_orchestrator.orchestrator.commit_service import (  # noqa: E402
    SERVICE_INTENT_REHANDED_OFF,
    MissionSpec,
    mission_account,
)
from agent_orchestrator.orchestrator.event_handler import (  # noqa: E402
    MAX_SERVICE_BLOCKER_SECONDS,
    MAX_SERVICE_REHANDOFFS,
    Orchestrator,
)
from agent_orchestrator.runtime.agent_worker import user_message_json  # noqa: E402
from agent_orchestrator.runtime.assembly import OrchestratorConfig  # noqa: E402
from agent_orchestrator.storage.htn_store import HtnStore  # noqa: E402
from agent_orchestrator.testing.fixtures import (  # noqa: E402
    RoleScriptedProvider,
)
from simple_harness.agents import AgentConfig, AgentLimits, AgentTurnState  # noqa: E402
from simple_harness.contracts import RunId  # noqa: E402
from simple_harness.providers.errors import ProviderTransportError  # noqa: E402

OPEN = ("PENDING", "CLAIMED", "AGENT_CREATED", "SUBMITTED")
LIMIT = 0.3  # seconds; ``stall_seconds`` below the 300 s ceiling, so it is the bound


def _transport_loss(request: Any) -> str:
    """A script step that fails the way the Grok lane failed: after the hand-off."""

    raise ProviderTransportError(public_message="scripted transport loss after handoff")


def _config(evidence: Path, **overrides: Any) -> OrchestratorConfig:
    values: dict[str, Any] = {
        "evidence_root": evidence,
        "max_concurrency": 3,
        "test_timeout_seconds": 60,
        "max_planning_attempts": 2,
        "stall_seconds": LIMIT,
    }
    values.update(overrides)
    return OrchestratorConfig(**values)


def _plain_world(tmp_path, *, key: str):
    evidence = Path(tmp_path) / "evidence"
    evidence.mkdir(parents=True, exist_ok=True)
    world = e2e.build_world(evidence, key=key)
    adopt = e2e._proposal_text(world.contract)
    world.store.close()
    return world, adopt, evidence


def _events(loop: Orchestrator, mission_id: str) -> list[Any]:
    return list(loop.store.list_events(mission_id))


def _rehandoffs(loop: Orchestrator, mission_id: str) -> list[dict[str, Any]]:
    return [
        dict(item.payload)
        for item in _events(loop, mission_id)
        if item.type == SERVICE_INTENT_REHANDED_OFF
    ]


def _invocation_states(loop: Orchestrator, intent) -> dict[str, list[str]]:  # type: ignore[no-untyped-def]
    """Provider invocation states per executor the subject ever had, from the runtime."""

    runtime = loop.bridge_for(intent).runtime
    agents = {intent.agent_id} | {
        item["previous_agent_id"] for item in _rehandoffs(loop, intent.mission_id)
        if item["subject_id"] == intent.subject_id
    }
    return {
        str(agent_id): [
            str(record.state)
            for record in runtime.uow.list_provider_invocations(RunId(str(agent_id)))
        ]
        for agent_id in sorted(a for a in agents if a)
    }


async def _run_until_done_or(loop: Orchestrator, seconds: float) -> bool:
    """``run()`` in a task; True when it returned within ``seconds``."""

    running = asyncio.create_task(loop.run(max_cycles=400))
    done, _pending = await asyncio.wait({running}, timeout=seconds)
    if running in done:
        running.result()
        return True
    running.cancel()
    try:
        await running
    except asyncio.CancelledError:
        pass
    return False


# ======================================================================================
# 1. the Planner: one re-hand-off, then the answer
# ======================================================================================


def test_a_planner_turn_blocked_on_an_unknown_outcome_is_rehanded_off_once_and_answers(
    tmp_path,
) -> None:
    """The red test for the wait.  Before P2.3f ``run()`` never returns here."""

    world, adopt, evidence = _plain_world(tmp_path, key="p23f-planner-rehandoff")
    provider = RoleScriptedProvider({"planner": [_transport_loss, adopt]})

    async def case() -> dict[str, Any]:
        async with Orchestrator(_config(evidence), provider, poll_interval=0.02) as loop:
            world.env.semantics = HtnStore(loop.store)
            loop.install_hierarchical(planning=world.env)
            mission_id = world.mission.id
            await loop._try_planner_intent(mission_id, ordinal=1)
            returned = await _run_until_done_or(loop, seconds=10.0)
            intent = loop.store.get_intent_for_subject(f"{mission_id}:planner:1")
            assert intent is not None
            return {
                "returned": returned,
                "types": [item.type for item in _events(loop, mission_id)],
                "rehandoffs": _rehandoffs(loop, mission_id),
                "intent_state": intent.state,
                "creation_key": intent.creation_key,
                "invocations": _invocation_states(loop, intent),
                "unknown_on_old_executor": any(
                    loop.bridge_for(intent).has_unknown_charge(agent_id=agent)
                    for agent in _invocation_states(loop, intent)
                    if agent != intent.agent_id
                ),
                "released": [
                    dict(item.payload)
                    for item in _events(loop, mission_id)
                    if item.type == "BudgetReleased"
                    and item.payload.get("subject_id") == f"{mission_id}:planner:1"
                ],
                "status": loop.store.get_mission(mission_id).status,
                "planner_calls": provider.by_role.get("planner", 0),
                "agents_created": sum(
                    1 for item in _events(loop, mission_id) if item.type == "AgentCreated"
                ),
            }

    outcome = asyncio.run(case())
    assert outcome["returned"] is True, (
        "the Planner intent stayed SUBMITTED on an unknown Provider outcome: "
        f"{outcome['types']} rehandoffs={outcome['rehandoffs']}"
    )
    assert len(outcome["rehandoffs"]) == 1, outcome["rehandoffs"]
    record = outcome["rehandoffs"][0]
    assert record["reason"] == "provider_outcome_unknown"
    assert record["rehandoff"] == 1 and record["kind"] == "plan" and record["role"] is None
    assert record["previous_agent_id"] and record["previous_turn_id"]
    assert record["detail"]["waited_seconds"] >= LIMIT
    assert record["detail"]["blocker"]["kind"] == "provider"
    assert outcome["planner_calls"] == 2, "the same question, asked of a second executor"
    assert outcome["creation_key"].endswith(":rehandoff:1")
    # The second executor's creation is on the record, not swallowed by the first's key.
    assert outcome["agents_created"] == 2, outcome["types"]
    assert "PlanRevisionCommitted" in outcome["types"], outcome["types"]
    assert outcome["intent_state"] == "SETTLED"
    assert outcome["status"] is not MissionStatus.PLANNING
    # Honest accounting: the abandoned executor's invocation is UNKNOWN in the runtime
    # ledger and is never imported as a charge; only the executor that answered is.
    states = outcome["invocations"]
    assert sorted(v for values in states.values() for v in values) == ["succeeded", "unknown"]
    assert outcome["unknown_on_old_executor"] is True
    assert outcome["released"] and outcome["released"][0]["settled_tokens"] == 150, (
        outcome["released"]
    )


def test_a_second_unknown_outcome_ends_the_planner_round_through_the_ladder(tmp_path) -> None:
    """Two executors, two unknowns: the round is rejected honestly and the ladder decides.

    With ``max_planning_attempts=1`` there is no next rung, so the Mission ends in
    PLANNING with ``planning_failure.reason == provider_outcome_unknown`` — a decided
    failure, not an idle one.
    """

    world, _adopt, evidence = _plain_world(tmp_path, key="p23f-planner-twice")
    provider = RoleScriptedProvider({"planner": [_transport_loss, _transport_loss]})

    async def case() -> dict[str, Any]:
        config = _config(evidence, max_planning_attempts=1)
        async with Orchestrator(config, provider, poll_interval=0.02) as loop:
            world.env.semantics = HtnStore(loop.store)
            loop.install_hierarchical(planning=world.env)
            mission_id = world.mission.id
            await loop._try_planner_intent(mission_id, ordinal=1)
            returned = await _run_until_done_or(loop, seconds=10.0)
            final = loop.store.get_mission(mission_id)
            return {
                "returned": returned,
                "types": [item.type for item in _events(loop, mission_id)],
                "rehandoffs": _rehandoffs(loop, mission_id),
                "rejections": [
                    dict(item.payload)
                    for item in _events(loop, mission_id)
                    if item.type == "PlanningRejected"
                ],
                "status": final.status,
                "report": dict(final.final_report or {}),
                "planner_calls": provider.by_role.get("planner", 0),
                "open": [
                    item.subject_id
                    for item in loop.store.list_intents(*OPEN)
                    if item.mission_id == mission_id
                ],
            }

    outcome = asyncio.run(case())
    assert outcome["returned"] is True, outcome["types"]
    assert len(outcome["rehandoffs"]) == MAX_SERVICE_REHANDOFFS == 1, outcome["rehandoffs"]
    assert outcome["planner_calls"] == 2, "exactly one retry, never a loop"
    assert outcome["rejections"] and outcome["rejections"][0]["reason"] == (
        "provider_outcome_unknown"
    ), outcome["rejections"]
    assert outcome["rejections"][0]["detail"]["rehandoffs"] == 1
    assert outcome["status"] is MissionStatus.FAILED, outcome["types"]
    assert outcome["report"]["planning_failure"]["reason"] == "provider_outcome_unknown"
    assert outcome["open"] == [], outcome["open"]


# ======================================================================================
# 2. the MethodSynthesizer: UNANSWERED, and the wait it caused ends
# ======================================================================================


def test_a_synthesizer_blocked_twice_is_recorded_unanswered_and_ends_the_wait(
    tmp_path,
) -> None:
    """P2.3d made a spent ladder *wait* for a synthesis round.  That wait has to end.

    Both Planner rungs are refused quickly; the synthesiser's executor is unknown
    twice.  The round is recorded ``UNANSWERED`` (not ``UNREADABLE`` — nobody read
    anything) and ``_after_synthesis_round`` ends the Mission the same way a refused
    proposal would: ``method_synthesis_refused``.
    """

    world, invented, config = saturation_world(tmp_path, key="p23f-synth-twice")
    del invented
    provider = RoleScriptedProvider(
        {
            "planner": ["nothing to propose", "still nothing"],
            "method_synthesizer": [_transport_loss, _transport_loss],
        }
    )

    async def case() -> dict[str, Any]:
        async with Orchestrator(config, provider, poll_interval=0.02) as loop:
            world.env.semantics = HtnStore(loop.store)
            loop.install_hierarchical(planning=world.env)
            mission_id = world.mission.id
            await loop._try_planner_intent(mission_id, ordinal=1)
            returned = await _run_until_done_or(loop, seconds=12.0)
            final = loop.store.get_mission(mission_id)
            return {
                "returned": returned,
                "types": [item.type for item in _events(loop, mission_id)],
                "rehandoffs": _rehandoffs(loop, mission_id),
                "synthesis": [
                    dict(item.payload)
                    for item in _events(loop, mission_id)
                    if item.type == "MethodSynthesisRoundRecorded"
                ],
                "status": final.status,
                "report": dict(final.final_report or {}),
                "roles": dict(provider.by_role),
            }

    outcome = asyncio.run(case())
    assert outcome["returned"] is True, outcome["types"]
    assert [item["role"] for item in outcome["rehandoffs"]] == ["method_synthesizer"], (
        outcome["rehandoffs"]
    )
    assert outcome["roles"].get("method_synthesizer") == 2, outcome["roles"]
    assert outcome["synthesis"] and outcome["synthesis"][0]["admitted"] is False
    assert outcome["synthesis"][0]["verdict"] == "UNANSWERED", outcome["synthesis"]
    assert outcome["status"] is MissionStatus.FAILED, outcome["types"]
    assert outcome["report"]["planning_failure"]["reason"] == "method_synthesis_refused"


def saturation_world(tmp_path, *, key: str):
    """The H-L3-C1 opening (gated method, two readings) with this file's bound."""

    evidence = Path(tmp_path) / "evidence"
    evidence.mkdir(parents=True, exist_ok=True)
    world = saturation._gated_world(evidence, key=key)
    for ordinal in (1, 2):
        saturation._observe(world, observer="plan.observer", ordinal=ordinal)
    invented = saturation._free_method()
    world.store.close()
    return world, invented, _config(evidence)


# ======================================================================================
# 3. the Critic: the runner's own wait, with the same two steps in it
# ======================================================================================


async def _critic_intent(loop: Orchestrator, mission_id: str, *, subject: str):
    """A Critic intent the way ``_run_critic`` writes one, without a workspace."""

    decision = loop._route_service("critic", mission_id)
    config = AgentConfig(
        name="critic-1",
        instructions="[role:critic]\nJudge the artefacts you are shown.",
        model_profile_ref=decision.profile_id,
        tool_names=(),
        limits=AgentLimits(
            max_model_calls_per_turn=2, max_tool_calls_per_turn=1, turn_deadline_seconds=60
        ),
    )
    message = user_message_json("[role:critic] judge")
    return loop.commit.create_service_intent(
        kind="critic",
        subject_id=subject,
        mission_id=mission_id,
        account_id=mission_account(mission_id),
        creation_key=subject,
        input_id="attempt-input",
        input_hash="0" * 64,
        config={
            "agent_config": config.to_json(),
            "message": message,
            "attempt_id": "view-p23f",
            "context_version": "ctx-p23f",
            "prompt_version": "critic-test",
            **loop._service_config(decision),
        },
        reservation=loop._reservation(1_000, decision.profile_id),
    )


def test_a_critic_turn_blocked_on_an_unknown_outcome_is_rehanded_off_and_answers(
    tmp_path,
) -> None:
    world, _adopt, evidence = _plain_world(tmp_path, key="p23f-critic-rehandoff")
    provider = RoleScriptedProvider({"critic": [_transport_loss, "PASS"]})

    async def case() -> dict[str, Any]:
        async with Orchestrator(_config(evidence), provider, poll_interval=0.02) as loop:
            world.env.semantics = HtnStore(loop.store)
            loop.install_hierarchical(planning=world.env)
            mission_id = world.mission.id
            subject = f"{mission_id}:critic-p23f:1"
            intent = await _critic_intent(loop, mission_id, subject=subject)
            started = loop.store.now
            intent, result = await asyncio.wait_for(
                loop._await_service_turn(intent, started + 10.0, attempt_id=None), timeout=15
            )
            return {
                "elapsed": loop.store.now - started,
                "state": None if result is None else result.state,
                "intent_state": intent.state,
                "rehandoffs": _rehandoffs(loop, mission_id),
                "critic_calls": provider.by_role.get("critic", 0),
                "invocations": _invocation_states(loop, intent),
            }

    outcome = asyncio.run(case())
    assert outcome["state"] is AgentTurnState.COMMITTED, outcome
    assert outcome["critic_calls"] == 2
    assert [item["kind"] for item in outcome["rehandoffs"]] == ["critic"], outcome["rehandoffs"]
    assert outcome["intent_state"] == "SUBMITTED", "the runner collects and settles it"
    assert outcome["elapsed"] < 5.0, "the wait is the bound, not the window"
    states = outcome["invocations"]
    assert sorted(v for values in states.values() for v in values) == ["succeeded", "unknown"]


def test_a_critic_blocked_twice_is_handed_back_to_the_runners_did_not_answer_path(
    tmp_path,
) -> None:
    """``result is None`` well before the window closes: the runner's existing door."""

    world, _adopt, evidence = _plain_world(tmp_path, key="p23f-critic-twice")
    provider = RoleScriptedProvider({"critic": [_transport_loss, _transport_loss]})

    async def case() -> dict[str, Any]:
        async with Orchestrator(_config(evidence), provider, poll_interval=0.02) as loop:
            world.env.semantics = HtnStore(loop.store)
            loop.install_hierarchical(planning=world.env)
            mission_id = world.mission.id
            subject = f"{mission_id}:critic-p23f:1"
            intent = await _critic_intent(loop, mission_id, subject=subject)
            started = loop.store.now
            intent, result = await asyncio.wait_for(
                loop._await_service_turn(intent, started + 30.0, attempt_id=None), timeout=15
            )
            return {
                "elapsed": loop.store.now - started,
                "result": result,
                "intent_state": intent.state,
                "rehandoffs": _rehandoffs(loop, mission_id),
                "critic_calls": provider.by_role.get("critic", 0),
            }

    outcome = asyncio.run(case())
    assert outcome["result"] is None
    assert outcome["critic_calls"] == 2, "one retry, then the runner's door"
    assert len(outcome["rehandoffs"]) == 1, outcome["rehandoffs"]
    assert outcome["intent_state"] == "SUBMITTED", (
        "the runner keeps SUBMITTED for after-stop collection, exactly as before"
    )
    assert outcome["elapsed"] < 5.0, "the 30 s window was not what ended the wait"


# ======================================================================================
# 4. the bound, and the line that is not moved
# ======================================================================================


def test_the_bound_is_the_smaller_of_stall_seconds_and_the_ceiling(tmp_path) -> None:
    evidence = Path(tmp_path) / "evidence"
    evidence.mkdir(parents=True, exist_ok=True)
    assert MAX_SERVICE_BLOCKER_SECONDS == 300.0
    small = Orchestrator(_config(evidence, stall_seconds=120.0), RoleScriptedProvider({}))
    assert small._service_blocker_limit == 120.0
    large = Orchestrator(_config(evidence, stall_seconds=1800.0), RoleScriptedProvider({}))
    assert large._service_blocker_limit == 300.0


def test_a_legacy_mission_is_not_rehanded_off(tmp_path) -> None:
    """The legacy Planner wait is the executor's, byte for byte as before.

    Same transport loss, same bound elapsed several times over: no
    ``ServiceIntentRehandedOff``, the intent still SUBMITTED, one Provider call.
    """

    evidence = Path(tmp_path) / "evidence"
    evidence.mkdir(parents=True, exist_ok=True)
    provider = RoleScriptedProvider({"planner": [_transport_loss]})

    async def case() -> dict[str, Any]:
        async with Orchestrator(_config(evidence), provider, poll_interval=0.02) as loop:
            mission = await loop.submit_mission(
                MissionSpec(
                    goal="legacy goal",
                    success_criteria=("file:a.md",),
                    tenant_id="tenant-p23f",
                    idempotency_key="p23f-legacy",
                )
            )
            returned = await _run_until_done_or(loop, seconds=LIMIT * 6)
            intent = loop.store.get_intent_for_subject(f"{mission.id}:planner:1")
            return {
                "returned": returned,
                "rehandoffs": _rehandoffs(loop, mission.id),
                "intent_state": None if intent is None else intent.state,
                "planner_calls": provider.by_role.get("planner", 0),
                "types": [item.type for item in _events(loop, mission.id)],
            }

    outcome = asyncio.run(case())
    assert outcome["rehandoffs"] == [], outcome
    assert outcome["intent_state"] == "SUBMITTED", outcome
    assert outcome["planner_calls"] == 1, outcome
    assert outcome["returned"] is False, "the legacy loop keeps waiting, as it did"
    assert SERVICE_INTENT_REHANDED_OFF not in outcome["types"]
