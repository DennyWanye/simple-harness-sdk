"""Committed H1-I decisions replay before authority and mutation checks."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from test_h1i_production_entry import (
    _config,
    _events,
    _open_planner_round,
    _refine_reply,
    _seed_new_protocol,
)

from agent_orchestrator.api.planning_authorization import PlanningAuthorizationApi
from agent_orchestrator.contracts.planning_decisions import PlanningDecisionStatus
from agent_orchestrator.governance.permissions import Principal
from agent_orchestrator.orchestrator.event_handler import Orchestrator
from agent_orchestrator.storage.planning_decision_store import PlanningDecisionStore
from agent_orchestrator.storage.store import StoreConflict
from agent_orchestrator.testing.fixtures import RoleScriptedProvider


def _receipt(loop: Orchestrator, mission_id: str, intent_id: str) -> tuple[Any, int, int, int, int]:
    decision = PlanningDecisionStore(loop.store).get_planning_decision_by_attempt(intent_id, 0)
    assert decision is not None
    action_count = loop.store.connection.execute(
        "SELECT COUNT(*) FROM actions WHERE mission_id = ?", (mission_id,)
    ).fetchone()[0]
    return (
        decision,
        len(_events(loop, mission_id, "PlanRevisionCommitted")),
        len(
            loop.store.list_intents(
                "PENDING", "CLAIMED", "AGENT_CREATED", "SUBMITTED", "SETTLED", "FAILED"
            )
        ),
        int(action_count),
        len(loop.store.list_events(mission_id)),
    )


def test_committed_refine_replays_after_grant_revocation_without_new_mutation(tmp_path) -> None:
    async def case() -> None:
        async with Orchestrator(_config(tmp_path), RoleScriptedProvider({"planner": []})) as loop:
            mission, _env, _contract, dispatch = _seed_new_protocol(
                loop, tmp_path, key="h1i-committed-replay"
            )
            opener = await _open_planner_round(loop, mission, dispatch, ordinal=1)
            api = PlanningAuthorizationApi(
                loop.store, tenant_id=mission.tenant_id, principal=Principal(loop._owner)
            )
            grant = api.issue(
                mission.id, command_id="grant-committed-replay", request_id=opener.intent_id
            )
            raw = _refine_reply(opener.config["planning_package"])
            await loop._collect_plan_decision(opener, object(), mission, raw, dispatch)
            before = _receipt(loop, mission.id, opener.intent_id)
            assert before[0]["status"] == str(PlanningDecisionStatus.COMMITTED)

            api.revoke(
                grant.grant_id,
                expected_revision=1,
                command_id="revoke-committed-replay",
                reason="replay must not need live authority",
            )
            before = _receipt(loop, mission.id, opener.intent_id)
            await loop._collect_plan_decision(opener, object(), mission, raw, dispatch)
            after = _receipt(loop, mission.id, opener.intent_id)
            assert after == before
            assert PlanningDecisionStore(loop.store).get_planning_decision(
                before[0]["decision_id"]
            )["status"] == str(PlanningDecisionStatus.COMMITTED)

    asyncio.run(case())


def test_committed_refine_rejects_different_raw_at_same_ordinal_without_rewriting_receipt(
    tmp_path,
) -> None:
    async def case() -> None:
        async with Orchestrator(_config(tmp_path), RoleScriptedProvider({"planner": []})) as loop:
            mission, _env, _contract, dispatch = _seed_new_protocol(
                loop, tmp_path, key="h1i-committed-replay-conflict"
            )
            opener = await _open_planner_round(loop, mission, dispatch, ordinal=1)
            PlanningAuthorizationApi(
                loop.store, tenant_id=mission.tenant_id, principal=Principal(loop._owner)
            ).issue(
                mission.id,
                command_id="grant-committed-replay-conflict",
                request_id=opener.intent_id,
            )
            raw = _refine_reply(opener.config["planning_package"])
            await loop._collect_plan_decision(opener, object(), mission, raw, dispatch)
            before = _receipt(loop, mission.id, opener.intent_id)
            conflicting_raw = raw + "\n"  # Even equivalent JSON has a different raw identity.
            assert conflicting_raw != raw
            with pytest.raises(StoreConflict):
                await loop._collect_plan_decision(
                    opener, object(), mission, conflicting_raw, dispatch
                )
            assert _receipt(loop, mission.id, opener.intent_id) == before

    asyncio.run(case())
