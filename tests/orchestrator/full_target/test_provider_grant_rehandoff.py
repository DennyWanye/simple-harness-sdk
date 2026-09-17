# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""P2.3l / defect N5: an UNKNOWN provider grant must not lock the next hand-off.

Grok H-L4-M3-r2 / r3: Planner 1 handed off, the consumer adapter raised
``provider_error_after_handoff`` (0 tokens, invocation UNKNOWN), P2.3f waited 300 s
and re-handed the same subject off — and every subsequent planner admission was
``authority_rejected`` because the first grant was still HELD.  The Mission died
``planning_failed`` with 0 tokens, a 50 k reservation stuck, and
``budget_conserved`` false.

P2.3f's own suite never installed ``ProviderBudgetGuard`` (no token estimator), so
the unguarded path stayed green.  These tests turn the guard on.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_service_intent_provider_blocker as blocker  # noqa: E402

from agent_orchestrator.contracts.models import MissionStatus  # noqa: E402
from agent_orchestrator.contracts.state_machines import MissionStopReason  # noqa: E402
from agent_orchestrator.orchestrator.commit_service import mission_account  # noqa: E402
from agent_orchestrator.orchestrator.event_handler import Orchestrator  # noqa: E402
from agent_orchestrator.storage.htn_store import HtnStore  # noqa: E402
from agent_orchestrator.testing.fixtures import RoleScriptedProvider  # noqa: E402
from simple_harness.providers.errors import ProviderTransportError  # noqa: E402

HELD = ("RESERVED", "HANDED_OFF", "UNKNOWN")
OPEN = blocker.OPEN
LIMIT = blocker.LIMIT


class _Estimator:
    """A cheap unpriced estimator so ``ProviderBudgetGuard`` is actually installed."""

    fingerprint = "p23l-fixture-text-count-v1"
    bound_protocol = "p23l-fixture-text-only-v1"
    requires_prior_output_reserve = False

    def estimate_input_tokens(self, request: Any) -> int:
        del request
        return 10


def _transport_loss(request: Any) -> str:
    del request
    raise ProviderTransportError(public_message="scripted transport loss after handoff")


def _grants(loop: Orchestrator) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in loop.store.connection.execute(
            "SELECT subject_id, agent_id, state, total_upper, actual_tokens "
            "FROM provider_token_grants ORDER BY created_at, invocation_id"
        )
    ]


def _conservation(loop: Orchestrator, mission_id: str) -> dict[str, Any]:
    report = loop.commit.ledger.costs_report(mission_id)
    account = next(
        item
        for item in report["accounts"]
        if item["account_id"] == mission_account(mission_id)
    )
    remaining = int(account["remaining_tokens"] or 0)
    reserved = int(account["reserved_tokens"])
    settled = int(account["settled_tokens"])
    pool = int(account["limits"]["max_tokens"])
    return {
        "holds": remaining + reserved + settled == pool,
        "remaining": remaining,
        "reserved": reserved,
        "settled": settled,
        "pool": pool,
        "held_reservations": list(report["held_reservations"]),
        "open_reservations": [
            dict(row)
            for row in report["reservations"]
            if row["state"] != "SETTLED"
        ],
    }


def _open_loop(evidence: Path, provider: RoleScriptedProvider, **overrides: Any) -> Orchestrator:
    return Orchestrator(
        blocker._config(evidence, **overrides),
        provider,
        provider_token_estimator=_Estimator(),
        poll_interval=0.02,
    )


def test_a_transport_unknown_grant_is_released_before_rehandoff_and_the_mission_continues(
    tmp_path,
) -> None:
    """First planner invoke dies after hand-off; the second must be admitted and answer.

    Before the fix ``recover()`` compared the live intent (rewritten by rehandoff) with
    the UNKNOWN grant's old agent and denied the second acquire as
    ``authority_rejected``.  The Mission never got a plan.
    """

    world, adopt, evidence = blocker._plain_world(tmp_path, key="p23l-n5-rehandoff")
    provider = RoleScriptedProvider({"planner": [_transport_loss, adopt]})

    async def case() -> dict[str, Any]:
        async with _open_loop(evidence, provider) as loop:
            world.env.semantics = HtnStore(loop.store)
            loop.install_hierarchical(planning=world.env)
            mission_id = world.mission.id
            await loop._try_planner_intent(mission_id, ordinal=1)
            returned = await blocker._run_until_done_or(loop, seconds=10.0)
            intent = loop.store.get_intent_for_subject(f"{mission_id}:planner:1")
            assert intent is not None
            return {
                "returned": returned,
                "types": [item.type for item in blocker._events(loop, mission_id)],
                "rehandoffs": blocker._rehandoffs(loop, mission_id),
                "grants": _grants(loop),
                "conservation": _conservation(loop, mission_id),
                "status": loop.store.get_mission(mission_id).status,
                "planner_calls": provider.by_role.get("planner", 0),
                "intent_state": intent.state,
            }

    outcome = asyncio.run(case())
    assert outcome["returned"] is True, outcome["types"]
    assert len(outcome["rehandoffs"]) == 1, outcome["rehandoffs"]
    assert outcome["planner_calls"] == 2, (
        "the second executor must be admitted after the UNKNOWN grant is released; "
        f"calls={outcome['planner_calls']} grants={outcome['grants']}"
    )
    assert "PlanRevisionCommitted" in outcome["types"], outcome["types"]
    assert outcome["intent_state"] == "SETTLED"
    assert outcome["status"] is not MissionStatus.PLANNING
    states = {row["state"] for row in outcome["grants"]}
    assert "UNKNOWN" not in states, outcome["grants"]
    assert outcome["conservation"]["holds"] is True, outcome["conservation"]
    assert outcome["conservation"]["held_reservations"] == [], outcome["conservation"]


def test_persistent_provider_unknown_stops_as_runtime_unavailable_and_releases_the_pool(
    tmp_path,
) -> None:
    """Two unknowns, no next rung: stop honestly, every reservation released, conservation holds.

    The transport never reached a model (0 tokens).  ``planning_failed`` is a lie —
    the Planner was never heard.  ``runtime_unavailable`` is the existing stop reason
    for a model service that stayed down.
    """

    world, _adopt, evidence = blocker._plain_world(tmp_path, key="p23l-n5-give-up")
    provider = RoleScriptedProvider({"planner": [_transport_loss, _transport_loss]})

    async def case() -> dict[str, Any]:
        async with _open_loop(evidence, provider, max_planning_attempts=1) as loop:
            world.env.semantics = HtnStore(loop.store)
            loop.install_hierarchical(planning=world.env)
            mission_id = world.mission.id
            await loop._try_planner_intent(mission_id, ordinal=1)
            returned = await blocker._run_until_done_or(loop, seconds=10.0)
            final = loop.store.get_mission(mission_id)
            return {
                "returned": returned,
                "types": [item.type for item in blocker._events(loop, mission_id)],
                "rehandoffs": blocker._rehandoffs(loop, mission_id),
                "grants": _grants(loop),
                "conservation": _conservation(loop, mission_id),
                "status": final.status,
                "stop_reason": final.stop_reason,
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
    assert len(outcome["rehandoffs"]) == 1, outcome["rehandoffs"]
    assert outcome["planner_calls"] == 2
    assert outcome["status"] is MissionStatus.FAILED, outcome["types"]
    assert outcome["stop_reason"] == str(MissionStopReason.RUNTIME_UNAVAILABLE), (
        f"stop_reason={outcome['stop_reason']!r} report={outcome['report']}"
    )
    assert outcome["report"]["planning_failure"]["reason"] == "provider_outcome_unknown"
    assert outcome["open"] == [], outcome["open"]
    assert all(row["state"] not in HELD for row in outcome["grants"]), outcome["grants"]
    assert outcome["conservation"]["holds"] is True, outcome["conservation"]
    assert outcome["conservation"]["reserved"] == 0, outcome["conservation"]
    assert outcome["conservation"]["held_reservations"] == [], outcome["conservation"]
    assert outcome["conservation"]["open_reservations"] == [], outcome["conservation"]


def _usage_rows(loop: Orchestrator, mission_id: str) -> list[dict[str, Any]]:
    return [dict(row) for row in loop.commit.ledger.costs_report(mission_id)["usage"]]


def _unknown_imported(loop: Orchestrator, subject_id: str) -> int:
    row = loop.store.connection.execute(
        "SELECT COUNT(*) FROM imported_usage WHERE subject_id=? AND unknown=1",
        (subject_id,),
    ).fetchone()
    return int(row[0])


def test_give_up_keeps_unknown_calls_on_the_ledger_and_does_not_settle_them_as_zero(
    tmp_path,
) -> None:
    """P1-1: releasing the grant is not permission to write the call as 0 tokens.

    Conservation still holds (remaining + reserved + settled == pool) with the
    unknown rows sitting beside settled facts, not inside them.
    """

    world, _adopt, evidence = blocker._plain_world(tmp_path, key="p23l-n5-unknown-kept")
    provider = RoleScriptedProvider({"planner": [_transport_loss, _transport_loss]})

    async def case() -> dict[str, Any]:
        async with _open_loop(evidence, provider, max_planning_attempts=1) as loop:
            world.env.semantics = HtnStore(loop.store)
            loop.install_hierarchical(planning=world.env)
            mission_id = world.mission.id
            await loop._try_planner_intent(mission_id, ordinal=1)
            returned = await blocker._run_until_done_or(loop, seconds=10.0)
            subject = f"{mission_id}:planner:1"
            return {
                "returned": returned,
                "usage": _usage_rows(loop, mission_id),
                "unknown_imported": _unknown_imported(loop, subject),
                "has_unknown": loop.commit.ledger.has_unknown_usage(subject),
                "conservation": _conservation(loop, mission_id),
                "known_tokens": loop.commit.ledger.usage_for(subject)[0]
                if hasattr(loop.commit.ledger, "known_usage_for")
                else None,
            }

    outcome = asyncio.run(case())
    assert outcome["returned"] is True
    assert outcome["unknown_imported"] >= 1, (
        "UNKNOWN invocations must land on imported_usage with unknown=1, "
        f"not vanish into a 0 settle: {outcome['usage']}"
    )
    assert any(int(row["unknown"]) == 1 for row in outcome["usage"]), outcome["usage"]
    assert all(
        int(row["input_tokens"]) + int(row["output_tokens"]) == 0 or int(row["unknown"]) == 0
        for row in outcome["usage"]
        if int(row["unknown"]) == 1
    ), outcome["usage"]
    assert outcome["conservation"]["holds"] is True, outcome["conservation"]
    assert outcome["conservation"]["reserved"] == 0, outcome["conservation"]
    assert outcome["conservation"]["held_reservations"] == [], outcome["conservation"]


def test_a_later_reconciled_unknown_call_is_imported_and_conservation_still_holds(
    tmp_path,
) -> None:
    """P1-1: an unknown imported_usage row is overwritten when the call is later known.

    Conservation (remaining + reserved + settled == pool) still holds; the unknown
    count drops to 0 and the usage_ref carries the reconciled tokens.
    """

    from agent_orchestrator.governance.budgets import UsageFact

    world, _adopt, evidence = blocker._plain_world(tmp_path, key="p23l-n5-reconcile")
    provider = RoleScriptedProvider({"planner": [_transport_loss, _transport_loss]})
    tokens = 5000

    async def case() -> dict[str, Any]:
        async with _open_loop(evidence, provider, max_planning_attempts=1) as loop:
            world.env.semantics = HtnStore(loop.store)
            loop.install_hierarchical(planning=world.env)
            mission_id = world.mission.id
            await loop._try_planner_intent(mission_id, ordinal=1)
            returned = await blocker._run_until_done_or(loop, seconds=10.0)
            subject = f"{mission_id}:planner:1"
            before = _usage_rows(loop, mission_id)
            unknown_refs = [
                row["usage_ref"] for row in before if int(row["unknown"]) == 1
            ]
            assert unknown_refs, before
            loop.commit.import_usage(
                subject,
                mission_id,
                [
                    UsageFact(ref, tokens - 1, 1, None, unknown=False)
                    for ref in unknown_refs
                ],
            )
            return {
                "returned": returned,
                "before": before,
                "usage": _usage_rows(loop, mission_id),
                "conservation": _conservation(loop, mission_id),
                "unknown_imported": _unknown_imported(loop, subject),
                "known_tokens": loop.commit.ledger.known_usage_for(subject)[0],
            }

    outcome = asyncio.run(case())
    assert outcome["returned"] is True, outcome
    assert outcome["unknown_imported"] == 0, outcome["usage"]
    n_unknown = sum(1 for row in outcome["before"] if int(row["unknown"]) == 1)
    assert outcome["known_tokens"] == tokens * n_unknown, outcome["usage"]
    charged = [
        row
        for row in outcome["usage"]
        if int(row["input_tokens"]) + int(row["output_tokens"]) == tokens
        and int(row["unknown"]) == 0
    ]
    assert charged, outcome["usage"]
    assert outcome["conservation"]["holds"] is True, outcome["conservation"]
    assert outcome["conservation"]["reserved"] == 0, outcome["conservation"]
