# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""P2.3p: consecutive after-handoff 0-token UNKNOWNs are bounded.

Grok H-L3-C2-r0/r1 (third batch): planner:1 and the synthesizer succeeded, then
every later planner invoke settled ``unknown`` / ``provider_error_after_handoff``
with 0 tokens.  P2.3f waited 300 s, re-handed off, the next invoke UNKNOWN'd in
milliseconds, ``PlanningRejected{provider_outcome_unknown}`` opened a **new**
planner ordinal, and the loop ran until the wall clock.  r0 stopped in PLANNING
with ``stop_reason=null`` and a hanging 50 k reservation; r1's Worker leaf sat
``blocked=true`` until 1800 s / ``budget_exhausted``.

P2.3l's ``runtime_unavailable`` only fires when the planning ladder is spent
*and* the Mission is still PLANNING.  Remaining rungs (or an ACTIVE Worker)
kept the hang unbounded.  This slice counts consecutive after-handoff 0-token
UNKNOWNs on the Mission and, at N = ``MAX_SERVICE_REHANDOFFS + 1`` (P2.3f's
per-subject retry width), stops as ``runtime_unavailable``, writes
``MissionFailed``, and releases UNKNOWN grants.  One such UNKNOWN, or an
UNKNOWN that carries tokens, still takes the P2.3f ladder.  Legacy is untouched.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_service_intent_provider_blocker as blocker  # noqa: E402
from test_htn_end_to_end import ROOT_DUTY  # noqa: E402
from test_provider_grant_rehandoff import (  # noqa: E402
    HELD,
    _conservation,
    _grants,
    _open_loop,
)

from agent_orchestrator.contracts.models import MissionStatus  # noqa: E402
from agent_orchestrator.contracts.state_machines import MissionStopReason  # noqa: E402
from agent_orchestrator.orchestrator.commit_service import (  # noqa: E402
    SERVICE_INTENT_REHANDED_OFF,
    MissionSpec,
)
from agent_orchestrator.orchestrator.event_handler import (  # noqa: E402
    MAX_CONSECUTIVE_AFTER_HANDOFF_UNKNOWNS,
    MAX_SERVICE_REHANDOFFS,
    Orchestrator,
)
from agent_orchestrator.storage.htn_store import HtnStore  # noqa: E402
from agent_orchestrator.testing.fixtures import (  # noqa: E402
    RoleScriptedProvider,
    UnknownAfterHandoff,
    critic_step,
    envelope_step,
)
from simple_harness.contracts import RunId, thaw_json  # noqa: E402
from simple_harness.providers.errors import ProviderTransportError  # noqa: E402

OPEN = blocker.OPEN
LIMIT = blocker.LIMIT


def _admit_root(loop: Orchestrator, mission_id: str) -> None:
    """Mission submitter admits the root duty (TG decision 9).  The Orchestrator does not."""

    loop.commit.admit_obligation_demand(
        mission_id,
        ROOT_DUTY,
        principal="mission-submitter",
        requester={"kind": "mission_root"},
        evidence={"mission_id": mission_id},
    )


def _unclassified(request: Any) -> str:
    """Handoff-after exception that is *not* in ``_DEFINITE_PROVIDER_FAILURES``."""

    del request
    raise UnknownAfterHandoff("scripted unclassified exception after handoff")


def _http_unclassified(request: Any) -> str:
    """Same path, but the exception carries an HTTP status the ledger should keep."""

    del request
    raise ProviderTransportError(
        public_message="scripted transport loss after handoff",
        status_code=418,
    )


def test_the_consecutive_bound_is_the_p23f_retry_width() -> None:
    """N is not a new config item: original hand-off + ``MAX_SERVICE_REHANDOFFS``."""

    assert MAX_SERVICE_REHANDOFFS == 1
    assert MAX_CONSECUTIVE_AFTER_HANDOFF_UNKNOWNS == MAX_SERVICE_REHANDOFFS + 1 == 2


def test_consecutive_after_handoff_unknowns_stop_even_when_the_ladder_has_rungs(
    tmp_path,
) -> None:
    """N unclassified after-handoff UNKNOWNs (0 tokens) stop the Mission.

    ``max_planning_attempts=3`` is the C2 shape: remaining rungs used to open
    planner:2 / :3 / :4.  After the fix the Mission is FAILED /
    ``runtime_unavailable`` with ``MissionFailed``, reservations released, and
    conservation holding.  It must not sit in PLANNING with ``stop_reason=null``.
    """

    world, _adopt, evidence = blocker._plain_world(tmp_path, key="p23p-ladder-stop")
    n = MAX_CONSECUTIVE_AFTER_HANDOFF_UNKNOWNS
    provider = RoleScriptedProvider({"planner": [_unclassified] * (n + 2)})

    async def case() -> dict[str, Any]:
        async with _open_loop(evidence, provider, max_planning_attempts=3) as loop:
            world.env.semantics = HtnStore(loop.store)
            loop.install_hierarchical(planning=world.env)
            mission_id = world.mission.id
            await loop._try_planner_intent(mission_id, ordinal=1)
            returned = await blocker._run_until_done_or(loop, seconds=10.0)
            final = loop.store.get_mission(mission_id)
            intent = loop.store.get_intent_for_subject(f"{mission_id}:planner:1")
            diagnostics = _invocation_diagnostics(loop, intent)
            return {
                "returned": returned,
                "types": [item.type for item in blocker._events(loop, mission_id)],
                "planner_subjects": sorted(
                    {
                        item.subject_id
                        for item in loop.store.list_intents(
                            "PENDING",
                            "CLAIMED",
                            "AGENT_CREATED",
                            "SUBMITTED",
                            "SETTLED",
                            "FAILED",
                        )
                        if item.mission_id == mission_id and ":planner:" in item.subject_id
                    }
                ),
                "planner_calls": provider.by_role.get("planner", 0),
                "status": final.status,
                "stop_reason": final.stop_reason,
                "report": dict(final.final_report or {}),
                "open": [
                    item.subject_id
                    for item in loop.store.list_intents(*OPEN)
                    if item.mission_id == mission_id
                ],
                "grants": _grants(loop),
                "conservation": _conservation(loop, mission_id),
                "diagnostics": diagnostics,
            }

    outcome = asyncio.run(case())
    assert outcome["returned"] is True, outcome["types"]
    assert outcome["status"] is MissionStatus.FAILED, outcome["types"]
    assert outcome["stop_reason"] == str(MissionStopReason.RUNTIME_UNAVAILABLE), (
        f"stop_reason={outcome['stop_reason']!r} report={outcome['report']}"
    )
    assert "MissionFailed" in outcome["types"], outcome["types"]
    assert outcome["open"] == [], outcome["open"]
    assert outcome["planner_calls"] == n, (
        "a remaining ladder rung must not open another planner ordinal: "
        f"calls={outcome['planner_calls']} subjects={outcome['planner_subjects']}"
    )
    assert all(":planner:2" not in subject for subject in outcome["planner_subjects"]), (
        outcome["planner_subjects"]
    )
    assert all(row["state"] not in HELD for row in outcome["grants"]), outcome["grants"]
    assert outcome["conservation"]["holds"] is True, outcome["conservation"]
    assert outcome["conservation"]["reserved"] == 0, outcome["conservation"]
    assert outcome["conservation"]["held_reservations"] == [], outcome["conservation"]
    classes = {item.get("error_class") for item in outcome["diagnostics"]}
    assert "UnknownAfterHandoff" in classes, outcome["diagnostics"]


def test_n_minus_one_after_handoff_unknown_then_recovery_completes(tmp_path) -> None:
    """N-1 unclassified after-handoff UNKNOWNs still take the P2.3f ladder to COMPLETED."""

    from test_nested_compound_composition import _accepting_reviewer

    world, adopt, evidence = blocker._plain_world(tmp_path, key="p23p-recover")
    passing_test = "def test_ok():\n    assert True\n"
    leaf = [
        ("workspace_write_file", {"path": "out/result.json", "content": "ok\n"}),
        ("workspace_write_file", {"path": "tests/test_ok.py", "content": passing_test}),
        ("workspace_write_file", {"path": "a.md", "content": "done\n"}),
        _leaf_envelope("result", "out/result.json"),
    ]
    review = [
        ("workspace_write_file", {"path": "out/verdict.json", "content": "PASS\n"}),
        ("workspace_write_file", {"path": "tests/test_ok.py", "content": passing_test}),
        ("workspace_write_file", {"path": "a.md", "content": "done\n"}),
        _leaf_envelope("verdict", "out/verdict.json"),
    ]
    provider = RoleScriptedProvider(
        {
            "planner": [_unclassified, adopt],
            "worker": (leaf + review) * 4,
            "critic": [critic_step(verdict="PASS", criteria_met=True)] * 8,
            "root_reviewer": [_accepting_reviewer] * 3,
        }
    )

    async def case() -> dict[str, Any]:
        async with _open_loop(evidence, provider, max_planning_attempts=3) as loop:
            world.env.semantics = HtnStore(loop.store)
            loop.install_hierarchical(planning=world.env)
            mission_id = world.mission.id
            _admit_root(loop, mission_id)
            await loop._try_planner_intent(mission_id, ordinal=1)
            returned = await blocker._run_until_done_or(loop, seconds=15.0)
            final = loop.store.get_mission(mission_id)
            return {
                "returned": returned,
                "types": [item.type for item in blocker._events(loop, mission_id)],
                "status": final.status,
                "stop_reason": final.stop_reason,
                "planner_calls": provider.by_role.get("planner", 0),
                "conservation": _conservation(loop, mission_id),
            }

    outcome = asyncio.run(case())
    assert outcome["returned"] is True, outcome["types"]
    assert outcome["status"] is MissionStatus.COMPLETED, (
        f"{outcome['status']} / {outcome['stop_reason']}: {outcome['types']}"
    )
    assert outcome["planner_calls"] == 2, outcome["planner_calls"]
    assert "MissionFailed" not in outcome["types"]
    assert outcome["conservation"]["holds"] is True, outcome["conservation"]


def test_a_legacy_mission_is_not_stopped_by_the_consecutive_bound(tmp_path) -> None:
    """The legacy Planner wait is the executor's.  Same unclassified after-handoff."""

    evidence = Path(tmp_path) / "evidence"
    evidence.mkdir(parents=True, exist_ok=True)
    provider = RoleScriptedProvider({"planner": [_unclassified, _unclassified]})

    async def case() -> dict[str, Any]:
        async with Orchestrator(
            blocker._config(evidence, max_planning_attempts=3),
            provider,
            poll_interval=0.02,
        ) as loop:
            mission = await loop.submit_mission(
                MissionSpec(
                    goal="legacy goal",
                    success_criteria=("file:a.md",),
                    tenant_id="tenant-p23p",
                    idempotency_key="p23p-legacy",
                )
            )
            returned = await blocker._run_until_done_or(loop, seconds=LIMIT * 8)
            final = loop.store.get_mission(mission.id)
            intent = loop.store.get_intent_for_subject(f"{mission.id}:planner:1")
            return {
                "returned": returned,
                "rehandoffs": blocker._rehandoffs(loop, mission.id),
                "intent_state": None if intent is None else intent.state,
                "planner_calls": provider.by_role.get("planner", 0),
                "status": final.status,
                "stop_reason": final.stop_reason,
                "types": [item.type for item in blocker._events(loop, mission.id)],
            }

    outcome = asyncio.run(case())
    assert outcome["rehandoffs"] == [], outcome
    assert outcome["intent_state"] == "SUBMITTED", outcome
    assert outcome["planner_calls"] == 1, outcome
    assert outcome["returned"] is False, "the legacy loop keeps waiting, as it did"
    assert outcome["status"] is not MissionStatus.FAILED
    assert outcome["stop_reason"] is None
    assert SERVICE_INTENT_REHANDED_OFF not in outcome["types"]
    assert "MissionFailed" not in outcome["types"]


def test_a_worker_leaf_consecutive_after_handoff_unknowns_stop_as_runtime_unavailable(
    tmp_path,
) -> None:
    """Worker attempts do not re-hand-off (P2.3f).  They also must not sit until 1800 s.

    After a committed plan, N consecutive after-handoff 0-token UNKNOWNs on the
    Worker leaf fail the Mission as ``runtime_unavailable`` and release the grant.
    """

    world, adopt, evidence = blocker._plain_world(tmp_path, key="p23p-worker")
    n = MAX_CONSECUTIVE_AFTER_HANDOFF_UNKNOWNS
    provider = RoleScriptedProvider(
        {
            "planner": [adopt],
            "worker": [_unclassified] * (n + 2),
            "critic": [critic_step(verdict="PASS", criteria_met=True)] * 4,
        }
    )

    async def case() -> dict[str, Any]:
        async with _open_loop(evidence, provider, max_planning_attempts=3) as loop:
            world.env.semantics = HtnStore(loop.store)
            loop.install_hierarchical(planning=world.env)
            mission_id = world.mission.id
            _admit_root(loop, mission_id)
            await loop._try_planner_intent(mission_id, ordinal=1)
            returned = await blocker._run_until_done_or(loop, seconds=15.0)
            final = loop.store.get_mission(mission_id)
            return {
                "returned": returned,
                "types": [item.type for item in blocker._events(loop, mission_id)],
                "status": final.status,
                "stop_reason": final.stop_reason,
                "report": dict(final.final_report or {}),
                "worker_calls": provider.by_role.get("worker", 0),
                "open": [
                    item.subject_id
                    for item in loop.store.list_intents(*OPEN)
                    if item.mission_id == mission_id
                ],
                "grants": _grants(loop),
                "conservation": _conservation(loop, mission_id),
            }

    outcome = asyncio.run(case())
    assert outcome["returned"] is True, outcome["types"]
    assert outcome["status"] is MissionStatus.FAILED, outcome["types"]
    assert outcome["stop_reason"] == str(MissionStopReason.RUNTIME_UNAVAILABLE), (
        f"stop_reason={outcome['stop_reason']!r} report={outcome['report']}"
    )
    assert "MissionFailed" in outcome["types"], outcome["types"]
    assert outcome["worker_calls"] >= n, outcome
    assert outcome["open"] == [], outcome["open"]
    assert all(row["state"] not in HELD for row in outcome["grants"]), outcome["grants"]
    assert outcome["conservation"]["holds"] is True, outcome["conservation"]
    assert outcome["conservation"]["reserved"] == 0, outcome["conservation"]


def test_after_handoff_unknown_keeps_error_class_and_http_status_on_the_ledger(
    tmp_path,
) -> None:
    """Diagnosability: short class name + HTTP status land in usage_json, never the body."""

    world, _adopt, evidence = blocker._plain_world(tmp_path, key="p23p-diag")
    provider = RoleScriptedProvider({"planner": [_http_unclassified, _http_unclassified]})

    async def case() -> dict[str, Any]:
        async with _open_loop(evidence, provider, max_planning_attempts=3) as loop:
            world.env.semantics = HtnStore(loop.store)
            loop.install_hierarchical(planning=world.env)
            mission_id = world.mission.id
            await loop._try_planner_intent(mission_id, ordinal=1)
            returned = await blocker._run_until_done_or(loop, seconds=10.0)
            intent = loop.store.get_intent_for_subject(f"{mission_id}:planner:1")
            return {
                "returned": returned,
                "diagnostics": _invocation_diagnostics(loop, intent),
                "status": loop.store.get_mission(mission_id).status,
            }

    outcome = asyncio.run(case())
    assert outcome["returned"] is True
    assert outcome["diagnostics"], "expected at least one after-handoff UNKNOWN"
    for item in outcome["diagnostics"]:
        assert item["error_code"] == "provider_error_after_handoff"
        assert item["error_class"] == "ProviderTransportError", item
        assert item["http_status"] == 418, item
        dumped = str(item["usage"])
        assert "scripted transport loss" not in dumped
        assert "Authorization" not in dumped
        assert "api_key" not in dumped


def _leaf_envelope(port: str, path: str):
    def step(request: Any) -> str:
        return envelope_step(
            summary=port,
            artifacts=[path],
            claims=[f"produced {port}"],
            override=lambda env: {**env, "outputs": {port: path}},
        )(request)

    return step


def _invocation_diagnostics(loop: Orchestrator, intent: Any) -> list[dict[str, Any]]:
    if intent is None or intent.agent_id is None:
        return []
    runtime = loop.bridge_for(intent).runtime
    agents = {intent.agent_id}
    for item in blocker._rehandoffs(loop, intent.mission_id):
        previous = item.get("previous_agent_id")
        if previous:
            agents.add(previous)
    rows: list[dict[str, Any]] = []
    for agent_id in agents:
        for record in runtime.uow.list_provider_invocations(RunId(str(agent_id))):
            if str(record.state) != "unknown":
                continue
            usage = record.usage_json
            payload = thaw_json(usage) if usage is not None else {}
            if not isinstance(payload, dict):
                payload = {}
            rows.append(
                {
                    "error_code": record.error_code,
                    "error_class": payload.get("error_class"),
                    "http_status": payload.get("http_status"),
                    "usage": payload,
                }
            )
    return rows
