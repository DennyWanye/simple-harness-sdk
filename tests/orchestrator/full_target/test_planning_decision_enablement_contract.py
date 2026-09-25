"""2026-09-25 主流程优化条目 2：模型看到的"可选决定"必须和解码器认的值一致。

9-23 有 7 局真实模型照抄请求包里的内部启用键 ``REPAIR/RETRY_SAME_METHOD`` 进 ``decision_type``，
被判 ``DECISION_TYPE_UNKNOWN``，任务失败。现在请求包分两个字段列出合法的 decision_type 和
合法的 repair_kind；内部键只在准入侧用，两者之间只有一对互逆函数。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agent_orchestrator.contracts.models import ContractError
from agent_orchestrator.contracts.planning_decisions import (
    H4_DECISION_ENABLEMENT,
    PlanningDecisionType,
    RepairKind,
    exposed_enablement,
    internal_enablement_keys,
)
from agent_orchestrator.orchestrator.event_handler import Orchestrator
from agent_orchestrator.runtime import role_templates
from agent_orchestrator.storage.planning_decision_store import PlanningDecisionStore
from agent_orchestrator.testing.fixtures import RoleScriptedProvider
from test_h1i_production_entry import _config, _seed_new_protocol


def test_exposed_values_are_legal_and_slash_free() -> None:
    types, kinds = exposed_enablement(H4_DECISION_ENABLEMENT)
    assert types and kinds
    assert all("/" not in value for value in types + kinds)
    assert {PlanningDecisionType(value) for value in types}  # every value decodes
    assert {RepairKind(value) for value in kinds}
    assert "REPAIR" in types


def test_exposed_and_internal_are_inverses() -> None:
    executable = frozenset(k for k, v in H4_DECISION_ENABLEMENT.items() if v.executable)
    types, kinds = exposed_enablement(H4_DECISION_ENABLEMENT)
    assert internal_enablement_keys(types, kinds) == executable


def test_internal_keys_refuse_kinds_without_repair() -> None:
    with pytest.raises(ContractError):
        internal_enablement_keys(["REFINE"], ["RETRY_SAME_METHOD"])
    with pytest.raises(ValueError):
        internal_enablement_keys(["REPAIR/RETRY_SAME_METHOD"], [])


def test_current_package_pairs_with_exactly_one_prompt() -> None:
    assert role_templates.PLANNING_DECISION_PACKAGE_VERSION == 8
    assert role_templates.PLANNING_DECISION_PROMPT_VERSION == role_templates.PLANNER_HIERARCHICAL_V11_VERSION
    assert role_templates.hierarchical_planner_pairing_is_valid("planner-hierarchical-v11", 8)
    assert not role_templates.hierarchical_planner_pairing_is_valid("planner-hierarchical-v10", 8)
    assert "enabled_repair_kinds" in role_templates.PLANNER_HIERARCHICAL_V11.instructions


def test_package_lists_types_and_kinds_and_admission_reads_internal_keys(tmp_path: Path) -> None:
    async def case() -> None:
        async with Orchestrator(_config(tmp_path), RoleScriptedProvider({"planner": []})) as loop:
            mission, _world, _binding, _dispatch = _seed_new_protocol(loop, tmp_path, key="enablement")
            binding = PlanningDecisionStore(loop.store).get_mission_protocol(mission.id)
            assert (binding["package_version"], binding["prompt_version"]) == (8, "planner-hierarchical-v11")
            intent = await loop._create_planner_intent(mission.id, ordinal=1)
            protocol = intent.config["planning_package"]["planning_protocol"]
            assert protocol["enabled_decision_types"] == sorted(protocol["enabled_decision_types"])
            assert all("/" not in v for v in protocol["enabled_decision_types"] + protocol["enabled_repair_kinds"])
            assert "REPAIR" in protocol["enabled_decision_types"]
            assert "RETRY_SAME_METHOD" in protocol["enabled_repair_kinds"]
            assert intent.config["planning_package"]["package_version"] == role_templates.PLANNING_DECISION_PACKAGE_LABEL

    asyncio.run(case())


def test_mission_bound_to_a_historical_package_fails_loudly(tmp_path: Path) -> None:
    async def case() -> None:
        async with Orchestrator(_config(tmp_path), RoleScriptedProvider({"planner": []})) as loop:
            mission, _world, _binding, _dispatch = _seed_new_protocol(loop, tmp_path, key="historical")
            store = PlanningDecisionStore(loop.store)
            with loop.store.transaction() as connection:  # rewrite the durable binding to package 7
                connection.execute(
                    "UPDATE mission_planning_protocols SET package_version=7, prompt_version='planner-hierarchical-v10'"
                    " WHERE mission_id=?", (mission.id,))
            assert store.get_mission_protocol(mission.id)["package_version"] == 7
            with pytest.raises(ContractError, match="unsupported planning package version"):
                loop._hierarchical_planner_template(mission.id)

    asyncio.run(case())
