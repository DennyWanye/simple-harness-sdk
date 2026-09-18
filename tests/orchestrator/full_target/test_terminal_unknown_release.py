# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""P2.3r: every Mission terminal path releases HELD/UNKNOWN grants.

Grok H-L3-C1-r0 (fourth batch, N9): the wall clock hit 1800 s while a Worker
verify attempt sat on an after-handoff UNKNOWN grant.  ``_runtime_exhausted``
failed the Mission and released the cascade, but did **not** call
``_release_mission_unknown_grants``.  The ledger kept reserved=100_000 and a
grant in ``UNKNOWN``; the Host runner then set ``budget_conserved=false``
because ``unknown_usage_calls=1``.

The honest accounting (P2.3l P1-1 / P2.3p) is: unknown usage stays on the
books as ``imported_usage.unknown=1``, the reservation is released, and
``remaining + reserved + settled == pool``.  ``usage_fully_known`` is the
field that means "every call's tokens are known"; it is not the same as
budget conservation.

N11: ``_handoff_unknown_diagnostics`` must walk ``__cause__`` / ``__context__``
so a ``MeteredProvider`` wrapper (``UnknownProviderUsage``) does not hide the
underlying class and HTTP status.
"""

from __future__ import annotations

import asyncio
import inspect
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_service_intent_provider_blocker as blocker  # noqa: E402
from test_after_handoff_unknown_bounded import _invocation_diagnostics  # noqa: E402
from test_htn_end_to_end import ROOT_DUTY  # noqa: E402
from test_provider_grant_rehandoff import (  # noqa: E402
    HELD,
    _conservation,
    _grants,
    _open_loop,
)

from agent_orchestrator.contracts.models import MissionStatus  # noqa: E402
from agent_orchestrator.contracts.state_machines import MissionStopReason  # noqa: E402
from agent_orchestrator.evaluation.metered_provider import (  # noqa: E402
    UnknownProviderUsage,
)
from agent_orchestrator.orchestrator.commit_service import MissionSpec  # noqa: E402
from agent_orchestrator.orchestrator.event_handler import Orchestrator  # noqa: E402
from agent_orchestrator.storage.htn_store import HtnStore  # noqa: E402
from agent_orchestrator.testing.fixtures import (  # noqa: E402
    RoleScriptedProvider,
    critic_step,
)
from simple_harness.execution.provider_admission import (  # noqa: E402
    ProviderAdmissionDenied,
    ProviderAdmissionFailure,
)
from simple_harness.providers.errors import ProviderTransportError  # noqa: E402

OPEN = blocker.OPEN
LIMIT = blocker.LIMIT


def _admit_root(loop: Orchestrator, mission_id: str) -> None:
    loop.commit.admit_obligation_demand(
        mission_id,
        ROOT_DUTY,
        principal="mission-submitter",
        requester={"kind": "mission_root"},
        evidence={"mission_id": mission_id},
    )


def _wrapped_http(request: Any) -> str:
    """MeteredProvider shape: wrap the transport error so diagnostics must unwrap."""

    del request
    try:
        raise ProviderTransportError(
            public_message="scripted transport loss after handoff",
            status_code=503,
        )
    except ProviderTransportError as error:
        raise UnknownProviderUsage("physical call failed; usage unknown") from error


def _admission_exhausted(request: Any) -> str:
    del request
    raise ProviderAdmissionDenied(
        public_message="scripted experiment budget exhausted",
        admission_detail=ProviderAdmissionFailure(reason_code="budget_exhausted"),
    )


def _assert_terminal_ledger(
    loop: Orchestrator,
    mission_id: str,
    *,
    expect_unknown: bool,
) -> dict[str, Any]:
    """Invariant: a terminal hierarchical Mission has no hanging reservation."""

    grants = _grants(loop)
    conservation = _conservation(loop, mission_id)
    report = loop.commit.ledger.costs_report(mission_id)
    final = loop.store.get_mission(mission_id)
    payload = dict(final.final_report or {})
    assert all(row["state"] not in HELD for row in grants), grants
    assert conservation["holds"] is True, conservation
    assert conservation["reserved"] == 0, conservation
    assert conservation["held_reservations"] == [], conservation
    assert report["usage_fully_known"] is (not expect_unknown), report
    assert report["budget_conserved"] is True, report
    assert payload.get("usage_fully_known") is (not expect_unknown), payload
    assert payload.get("budget_conserved") is True, payload
    if expect_unknown:
        assert any(int(row["unknown"]) == 1 for row in report["usage"]), report["usage"]
    return {
        "grants": grants,
        "conservation": conservation,
        "report": report,
        "payload": payload,
    }


def test_every_mission_terminal_writer_goes_through_the_ledger_hook() -> None:
    """One choke point: fail_mission / fail_planning / stop_task / cancel_mission."""

    from agent_orchestrator.orchestrator import event_handler

    source = inspect.getsource(event_handler.Orchestrator)
    assert source.count("self.commit.fail_mission(") == 1, (
        "every fail_mission site must go through _commit_fail_mission"
    )
    assert source.count("self.commit.fail_planning(") == 1, (
        "every fail_planning site must go through _commit_fail_planning"
    )
    assert source.count("self.commit.stop_task(") == 1, (
        "every stop_task site must go through _commit_stop_task"
    )
    assert source.count("self.commit.cancel_mission(") == 1, (
        "every cancel_mission site must go through _commit_cancel_mission"
    )
    assert "def _prepare_terminal_ledger" in source


def test_wall_clock_after_handoff_unknown_releases_grants_and_keeps_unknown_on_the_books(
    tmp_path,
) -> None:
    """C1-r0 shape: one after-handoff UNKNOWN, wall clock first, no hanging grant.

    ``stall_seconds`` is far above the wall clock so P2.3f / P2.3p do not fire.
    Diagnostics must unwrap ``UnknownProviderUsage`` to the transport class.
    """

    from test_htn_end_to_end import _proposal_text, build_world

    evidence = Path(tmp_path) / "evidence"
    evidence.mkdir(parents=True, exist_ok=True)
    world = build_world(evidence, key="p23r-wall", max_runtime_seconds=2)
    adopt = _proposal_text(world.contract)
    world.store.close()
    provider = RoleScriptedProvider(
        {
            "planner": [adopt],
            "worker": [_wrapped_http] * 4,
            "critic": [critic_step(verdict="PASS", criteria_met=True)] * 4,
        }
    )

    async def case() -> dict[str, Any]:
        async with _open_loop(
            evidence, provider, max_planning_attempts=3, stall_seconds=60
        ) as loop:
            world.env.semantics = HtnStore(loop.store)
            loop.install_hierarchical(planning=world.env)
            mission_id = world.mission.id
            _admit_root(loop, mission_id)
            await loop._try_planner_intent(mission_id, ordinal=1)
            returned = await blocker._run_until_done_or(loop, seconds=8.0)
            final = loop.store.get_mission(mission_id)
            worker = next(
                (
                    item
                    for item in loop.store.list_intents(
                        "PENDING",
                        "CLAIMED",
                        "AGENT_CREATED",
                        "SUBMITTED",
                        "SETTLED",
                        "FAILED",
                    )
                    if item.mission_id == mission_id and item.kind == "attempt"
                ),
                None,
            )
            ledger = _assert_terminal_ledger(loop, mission_id, expect_unknown=True)
            return {
                "returned": returned,
                "status": final.status,
                "stop_reason": final.stop_reason,
                "detail": dict((final.final_report or {}).get("detail") or {}),
                "types": [item.type for item in blocker._events(loop, mission_id)],
                "diagnostics": _invocation_diagnostics(loop, worker),
                **ledger,
            }

    outcome = asyncio.run(case())
    assert outcome["returned"] is True, outcome["types"]
    assert outcome["status"] is MissionStatus.FAILED, outcome["types"]
    assert outcome["stop_reason"] == str(MissionStopReason.BUDGET_EXHAUSTED), outcome
    assert outcome["detail"].get("dimension") == "runtime", outcome["detail"]
    assert "MissionFailed" in outcome["types"]
    assert outcome["diagnostics"], "expected an after-handoff UNKNOWN invocation"
    for item in outcome["diagnostics"]:
        assert item["error_code"] == "provider_error_after_handoff"
        assert item["error_class"] == "ProviderTransportError", item
        assert item["http_status"] == 503, item
        usage = item["usage"]
        assert usage.get("wrapper_class") == "UnknownProviderUsage", usage
        dumped = str(usage)
        assert "scripted transport loss" not in dumped
        assert "Authorization" not in dumped
        assert "api_key" not in dumped


def test_admission_denied_budget_exhausted_releases_grants(tmp_path) -> None:
    """C1-r1 / C2-r0 shape: provider_admission_denied after handoff, grant was UNKNOWN."""

    world, adopt, evidence = blocker._plain_world(tmp_path, key="p23r-admit")
    provider = RoleScriptedProvider(
        {
            "planner": [adopt],
            "worker": [_admission_exhausted] * 3,
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
            returned = await blocker._run_until_done_or(loop, seconds=8.0)
            final = loop.store.get_mission(mission_id)
            ledger = _assert_terminal_ledger(loop, mission_id, expect_unknown=False)
            return {
                "returned": returned,
                "status": final.status,
                "stop_reason": final.stop_reason,
                "detail": dict((final.final_report or {}).get("detail") or {}),
                "types": [item.type for item in blocker._events(loop, mission_id)],
                **ledger,
            }

    outcome = asyncio.run(case())
    assert outcome["returned"] is True, outcome["types"]
    assert outcome["status"] is MissionStatus.FAILED, outcome["types"]
    assert outcome["stop_reason"] == str(MissionStopReason.BUDGET_EXHAUSTED), outcome
    assert outcome["detail"].get("source_kind") == "provider_admission", outcome["detail"]
    assert "MissionFailed" in outcome["types"]


def test_planning_failed_has_no_hanging_reservation(tmp_path) -> None:
    world, _adopt, evidence = blocker._plain_world(tmp_path, key="p23r-plan-fail")
    provider = RoleScriptedProvider({"planner": ["not a proposal", "still not"]})

    async def case() -> dict[str, Any]:
        async with _open_loop(evidence, provider, max_planning_attempts=1) as loop:
            world.env.semantics = HtnStore(loop.store)
            loop.install_hierarchical(planning=world.env)
            mission_id = world.mission.id
            await loop._try_planner_intent(mission_id, ordinal=1)
            returned = await blocker._run_until_done_or(loop, seconds=8.0)
            final = loop.store.get_mission(mission_id)
            ledger = _assert_terminal_ledger(loop, mission_id, expect_unknown=False)
            return {
                "returned": returned,
                "status": final.status,
                "stop_reason": final.stop_reason,
                "types": [item.type for item in blocker._events(loop, mission_id)],
                **ledger,
            }

    outcome = asyncio.run(case())
    assert outcome["returned"] is True, outcome["types"]
    assert outcome["status"] is MissionStatus.FAILED, outcome["types"]
    assert outcome["stop_reason"] == str(MissionStopReason.PLANNING_FAILED), outcome


def test_runtime_unavailable_exports_usage_fully_known_distinct_from_conservation(
    tmp_path,
) -> None:
    """P2.3p stop still holds; the new field says usage is not fully known."""

    from agent_orchestrator.orchestrator.event_handler import (
        MAX_CONSECUTIVE_AFTER_HANDOFF_UNKNOWNS,
    )

    world, _adopt, evidence = blocker._plain_world(tmp_path, key="p23r-runtime")
    n = MAX_CONSECUTIVE_AFTER_HANDOFF_UNKNOWNS
    provider = RoleScriptedProvider({"planner": [_wrapped_http] * (n + 2)})

    async def case() -> dict[str, Any]:
        async with _open_loop(
            evidence, provider, max_planning_attempts=3, stall_seconds=LIMIT
        ) as loop:
            world.env.semantics = HtnStore(loop.store)
            loop.install_hierarchical(planning=world.env)
            mission_id = world.mission.id
            await loop._try_planner_intent(mission_id, ordinal=1)
            returned = await blocker._run_until_done_or(loop, seconds=10.0)
            final = loop.store.get_mission(mission_id)
            ledger = _assert_terminal_ledger(loop, mission_id, expect_unknown=True)
            return {
                "returned": returned,
                "status": final.status,
                "stop_reason": final.stop_reason,
                "types": [item.type for item in blocker._events(loop, mission_id)],
                **ledger,
            }

    outcome = asyncio.run(case())
    assert outcome["returned"] is True, outcome["types"]
    assert outcome["status"] is MissionStatus.FAILED, outcome["types"]
    assert outcome["stop_reason"] == str(MissionStopReason.RUNTIME_UNAVAILABLE), outcome
    assert outcome["report"]["usage_fully_known"] is False
    assert outcome["report"]["budget_conserved"] is True


def test_a_legacy_mission_does_not_gain_usage_fully_known_on_the_report(tmp_path) -> None:
    """Legacy goldens: the new fields are hierarchical-only."""

    evidence = Path(tmp_path) / "evidence"
    evidence.mkdir(parents=True, exist_ok=True)
    provider = RoleScriptedProvider({"planner": [_wrapped_http, _wrapped_http]})

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
                    tenant_id="tenant-p23r",
                    idempotency_key="p23r-legacy",
                )
            )
            returned = await blocker._run_until_done_or(loop, seconds=LIMIT * 8)
            final = loop.store.get_mission(mission.id)
            report = loop.commit.ledger.costs_report(mission.id)
            return {
                "returned": returned,
                "status": final.status,
                "stop_reason": final.stop_reason,
                "payload": dict(final.final_report or {}),
                "report_keys": set(report),
                "types": [item.type for item in blocker._events(loop, mission.id)],
            }

    outcome = asyncio.run(case())
    assert outcome["returned"] is False
    assert outcome["status"] is not MissionStatus.FAILED
    assert "usage_fully_known" not in outcome["payload"]
    assert "budget_conserved" not in outcome["payload"]
    assert "usage_fully_known" not in outcome["report_keys"]
    assert "budget_conserved" not in outcome["report_keys"]
    assert "MissionFailed" not in outcome["types"]


def test_cancel_mission_writes_usage_flags_on_a_hierarchical_report(tmp_path) -> None:
    """P2-1: cancel_mission is a terminal path; hierarchical final_report carries
    the two ledger flags."""

    from test_htn_end_to_end import committed  # noqa: PLC0415

    world = committed(tmp_path, key="p23q-cancel-flags", demand=True)
    loop_mission = world.mission.id
    cancelled = world.service.cancel_mission(loop_mission)
    report = dict(cancelled.final_report or {})
    assert cancelled.status is MissionStatus.CANCELLED
    assert "usage_fully_known" in report
    assert "budget_conserved" in report
    costs = world.service.ledger.costs_report(loop_mission)
    assert "usage_fully_known" in costs
    assert costs["budget_conserved"] is True
