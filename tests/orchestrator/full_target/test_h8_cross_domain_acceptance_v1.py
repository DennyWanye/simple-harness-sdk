from __future__ import annotations

import pytest

from agent_orchestrator.contracts.models import ContractError
from agent_orchestrator.planning.htn.cross_domain_acceptance import (
    CrossDomainAcceptancePlan,
    FourArm,
    ScenarioKind,
)


def test_plan_has_three_domains_and_48_scenarios_with_three_trials() -> None:
    plan = CrossDomainAcceptancePlan.build()
    assert plan.domains == ("appworld-v1", "code-v1", "drone-sim-v1")
    assert len(plan.scenarios) == 48
    assert plan.trials == 3
    for domain in plan.domains:
        for kind in ScenarioKind:
            expected = {"normal": 8, "repair": 4, "evidence-conflict": 2, "recovery": 2}
            assert sum(item.domain == domain and item.kind is kind for item in plan.scenarios) == expected[kind.value]


def test_four_arms_share_frozen_model_tools_and_budget() -> None:
    plan = CrossDomainAcceptancePlan.build(
        model_id="gpt-5.6-sol", toolset_hash="tools-v1", total_budget=1000
    )
    assert plan.arms == tuple(FourArm)
    assert plan.config_hash
    assert plan.validate_run_config(
        {"model_id": "gpt-5.6-sol", "toolset_hash": "tools-v1", "total_budget": 1000}
    )
    assert not plan.validate_run_config(
        {"model_id": "other", "toolset_hash": "tools-v1", "total_budget": 1000}
    )


def test_unknown_domain_and_bad_trial_count_fail_closed() -> None:
    with pytest.raises(ContractError):
        CrossDomainAcceptancePlan.build(domains=("code-v1",), trials=2)
    with pytest.raises(ContractError):
        CrossDomainAcceptancePlan.build(domains=("code-v1", "appworld-v1", "unknown-v1"))


def test_runner_and_result_validator_cover_every_frozen_cell() -> None:
    plan = CrossDomainAcceptancePlan.build()
    results = plan.run(
        lambda scenario, arm, trial: bool(scenario.scenario_id and arm and trial),
        config={
            "model_id": plan.model_id,
            "toolset_hash": plan.toolset_hash,
            "total_budget": plan.total_budget,
        },
    )
    assert len(results) == 48 * 4 * 3
    plan.validate_results(results)
    with pytest.raises(ContractError, match="duplicate"):
        plan.validate_results(results + (results[0],))
