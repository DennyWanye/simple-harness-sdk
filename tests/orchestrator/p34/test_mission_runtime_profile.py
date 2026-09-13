"""A Mission's explicit runtime pool survives policy-cache reuse and cold reopen."""

from __future__ import annotations

import asyncio

import pytest

from agent_orchestrator.api.facade import FacadeError, MissionControlV1
from agent_orchestrator.api.missions import MissionRequestError
from agent_orchestrator.contracts import Budget, ContractError
from agent_orchestrator.governance.permissions import Principal
from agent_orchestrator.orchestrator.commit_service import (
    CommitRejected,
    CommitService,
    MissionConflict,
    MissionSpec,
)
from agent_orchestrator.orchestrator.event_handler import Orchestrator
from agent_orchestrator.runtime.assembly import OrchestratorConfig
from agent_orchestrator.runtime.model_router import RoutingRules, RuntimeProfile
from agent_orchestrator.storage.store import Store
from agent_orchestrator.testing.fixtures import RoleScriptedProvider
from simple_harness.agents.context.budget import ContextPolicy

LONG = "deepseek-context-256k-v1"
LONG_512 = "deepseek-context-512k-v1"
PERSON = Principal("profile-test")


def _request(key: str, **extra):
    return {
        "goal": "Write NOTES.md",
        "success_criteria": ["file:NOTES.md"],
        "idempotency_key": key,
        "budget": {"max_tokens": 2_000_000, "max_attempts": 4},
        **extra,
    }


def _orchestrator(root, *, routing=None, include_long=True):
    provider = RoleScriptedProvider({})
    profiles = {
        "default": RuntimeProfile("default", provider, "agent-model"),
        LONG: RuntimeProfile(
            LONG,
            provider,
            "agent-model",
            context_policy=ContextPolicy(
                max_input_tokens=262_144, max_tool_result_tokens=16_384, render_slack_tokens=0
            ),
            default_max_output_tokens=8_192,
            max_output_tokens_ceiling=32_768,
        ),
        LONG_512: RuntimeProfile(
            LONG_512,
            provider,
            "agent-model",
            context_policy=ContextPolicy(
                max_input_tokens=524_288, max_tool_result_tokens=16_384, render_slack_tokens=0
            ),
            default_max_output_tokens=8_192,
            max_output_tokens_ceiling=32_768,
        ),
    }
    if not include_long:
        del profiles[LONG]
        del profiles[LONG_512]
    return Orchestrator(
        OrchestratorConfig(evidence_root=root),
        provider,
        profiles=profiles,
        routing=routing,
    )


def _route(orch, mission_id: str, role: str, kind: str | None = None) -> str:
    return orch._router_for(mission_id).route(role=role, task_kind=kind).profile_id


def test_public_create_freezes_profile_and_keeps_omitted_hash(tmp_path):
    async def run():
        async with _orchestrator(tmp_path / "library") as orch:
            control = MissionControlV1(orch, tenant_id="tenant", principal=PERSON)
            old = control.create(_request("old"))
            new = control.create(_request("new", runtime_profile_id=LONG))
            larger = control.create(_request("larger", runtime_profile_id=LONG_512))
            same = control.create(_request("new", runtime_profile_id=LONG))
            assert old["created"] and new["created"] and not same["created"]
            assert same["spec_hash"] == new["spec_hash"]
            assert (
                "runtime_profile_id" not in orch.store.get_mission(old["mission_id"]).final_report
            )
            assert (
                orch.store.get_mission(new["mission_id"]).final_report["runtime_profile_id"] == LONG
            )
            assert _route(orch, old["mission_id"], "critic") == "default"
            assert _route(orch, larger["mission_id"], "critic") == LONG_512
            for role, kind in (
                ("planner", None),
                ("worker", "code"),
                ("manager", None),
                ("critic", None),
                ("synthesizer", "code"),
                ("arbiter", "code"),
            ):
                assert _route(orch, new["mission_id"], role, kind) == LONG
            # Both Missions have the same policy version; the router cache must differ.
            assert orch.policy_version_of(old["mission_id"]) == orch.policy_version_of(
                new["mission_id"]
            )
            assert orch.policy_version_of(new["mission_id"]) == orch.policy_version_of(
                larger["mission_id"]
            )
            assert (
                orch._budget_floor(old["mission_id"])["min_task_tokens_with_critic_review"]
                == 14_192
            )
            assert orch._budget_floor(new["mission_id"])["min_task_tokens"] == 294_912
            assert (
                orch._budget_floor(new["mission_id"])["min_task_tokens_with_critic_review"]
                == 589_824
            )
            assert (
                orch._budget_floor(larger["mission_id"])["min_task_tokens_with_critic_review"]
                == 1_114_112
            )
            with pytest.raises(FacadeError) as changed:
                control.create(_request("new"))
            assert changed.value.code == "conflict"
            assert (
                MissionSpec(
                    goal="x", success_criteria=("file:x",), tenant_id="t", idempotency_key="k"
                )
                .to_json()
                .get("runtime_profile_id")
                is None
            )
            return old["mission_id"], new["mission_id"]

    old_id, new_id = asyncio.run(run())

    async def reopen():
        async with _orchestrator(tmp_path / "library") as orch:
            assert _route(orch, old_id, "worker", "code") == "default"
            assert _route(orch, new_id, "planner") == LONG
            assert _route(orch, new_id, "critic") == LONG
            assert _route(orch, old_id, "manager") == "default"

    asyncio.run(reopen())


def test_selected_mission_fails_closed_if_pool_or_route_changes_on_reopen(tmp_path):
    async def create():
        async with _orchestrator(tmp_path / "library") as orch:
            mission, _ = orch.create_mission(
                tenant_id="tenant", request=_request("selected", runtime_profile_id=LONG)
            )
            return mission.id

    mission_id = asyncio.run(create())

    async def missing_pool():
        async with _orchestrator(tmp_path / "library", include_long=False) as orch:
            with pytest.raises(ContractError, match="not configured"):
                _route(orch, mission_id, "critic")

    asyncio.run(missing_pool())

    async def conflicting_route():
        routing = RoutingRules(default="default", by_role={"critic": "default"})
        async with _orchestrator(tmp_path / "library", routing=routing) as orch:
            with pytest.raises(ContractError, match="policy route"):
                _route(orch, mission_id, "critic")

    asyncio.run(conflicting_route())


def test_missing_profile_and_bound_policy_conflict_roll_back(tmp_path):
    async def run():
        routing = RoutingRules(default="default", by_task_kind={"code": "default"})
        async with _orchestrator(tmp_path / "library", routing=routing) as orch:
            control = MissionControlV1(orch, tenant_id="tenant", principal=PERSON)
            with pytest.raises(FacadeError) as missing:
                control.create(_request("missing", runtime_profile_id="no-such-pool"))
            assert missing.value.code == "invalid_request"
            with pytest.raises(FacadeError) as conflict:
                control.create(_request("conflicting", runtime_profile_id=LONG))
            assert conflict.value.code == "invalid_request"
            assert "policy route" in str(conflict.value)
            assert orch.store.list_missions() == []

    asyncio.run(run())


def test_source_and_submit_paths_validate_selection(tmp_path):
    async def run():
        async with _orchestrator(tmp_path / "library") as orch:
            control = MissionControlV1(orch, tenant_id="tenant", principal=PERSON)
            receipt = control.create_with_sources(
                {
                    "mission": _request("source", runtime_profile_id=LONG),
                    "sources": [],
                }
            )
            assert _route(orch, receipt["mission_id"], "critic") == LONG
            with pytest.raises(MissionRequestError, match="not configured"):
                await orch.submit_mission(
                    MissionSpec(
                        goal="Write NOTES.md",
                        success_criteria=("file:NOTES.md",),
                        tenant_id="tenant",
                        idempotency_key="submit-missing",
                        runtime_profile_id="missing",
                    )
                )
            assert len(orch.store.list_missions()) == 1
            with pytest.raises(MissionConflict):
                orch.commit.create_mission(
                    MissionSpec(
                        goal="Write NOTES.md",
                        success_criteria=("file:NOTES.md",),
                        tenant_id="tenant",
                        idempotency_key="source",
                        allowed_tools=orch.config.deployment_policy.allowed_tools,
                        budget=Budget(max_tokens=2_000_000, max_attempts=4),
                        runtime_profile_id="default",
                    )
                )

    asyncio.run(run())


def test_bare_commit_cannot_bypass_profile_binding(tmp_path):
    store = Store.open(tmp_path / "bare.db")
    try:
        commit = CommitService(store)
        with pytest.raises(CommitRejected, match="requires an Orchestrator"):
            commit.create_mission(
                MissionSpec(
                    goal="Write NOTES.md",
                    success_criteria=("file:NOTES.md",),
                    tenant_id="tenant",
                    idempotency_key="bare",
                    runtime_profile_id=LONG,
                )
            )
        assert store.list_missions() == []
    finally:
        store.close()
