# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""A malformed Critic schema gets bounded feedback without reusing its verdict."""

from __future__ import annotations

import asyncio
import hashlib
import json

import pytest
from fixtures_provider import RoleScriptedProvider, envelope_step, graph_proposal_step, package_of
from graph_helpers7 import node, spec

from agent_orchestrator.contracts import Budget, ContractError, MissionStatus, TaskStatus
from agent_orchestrator.orchestrator.event_handler import Orchestrator
from agent_orchestrator.runtime.assembly import OrchestratorConfig
from agent_orchestrator.runtime.role_templates import CRITIC, CRITIC_V2, TEMPLATE_VERSIONS
from agent_orchestrator.storage.store import InjectedCrash
from agent_orchestrator.verification.critics import (
    critic_schema_retry_feedback,
    parse_critic_verdict,
)

MISSION_CRITERIA = ("file:mission-a.md", "file:mission-b.md", "file:mission-c.md")
TASK_CRITERION = "file:task.md"
FILES = ("task.md", "mission-a.md", "mission-b.md", "mission-c.md")
FILE_CONTENT = "# Independently reviewed fixture\n"
SCHEMA_FEEDBACK = [{
    "reason_code": "mission_criteria_mismatch",
    "expected_source": "mission_success_criteria",
    "excluded_source": "task_contract.success_criteria",
    "required_order": "exact",
}]


def _verdict(criteria: list[str]) -> str:
    body = {
        "verdict": "PASS",
        "findings": [],
        "mission_criteria": [
            {"criterion": criterion, "met": True, "reason": "read the verification copy"}
            for criterion in criteria
        ],
    }
    return "<critic_verdict>" + json.dumps(body) + "</critic_verdict>"


def test_critic_v2_bytes_and_strict_mission_schema_remain_frozen():
    assert CRITIC_V2.prompt_version == "critic-v2"
    assert hashlib.sha256(CRITIC_V2.instructions.encode()).hexdigest() == (
        "8eb51a32c06bfa16da88e4e89a28f48b50ce2e06969aec467abd803078a1c5ce"
    )
    assert TEMPLATE_VERSIONS["critic"]["critic-v2"] is CRITIC_V2
    assert CRITIC.prompt_version == "critic-v3"
    assert "task_contract.success_criteria" in CRITIC.instructions
    with pytest.raises(ContractError, match="mission_criteria") as rejected:
        parse_critic_verdict(
            _verdict([TASK_CRITERION, *MISSION_CRITERIA]),
            expected_criteria=MISSION_CRITERIA,
        )
    assert list(critic_schema_retry_feedback(rejected.value)) == SCHEMA_FEEDBACK
    for frozen_version in ("critic-v2", "doc-critic-v9", "critic-future"):
        assert critic_schema_retry_feedback(
            rejected.value, prompt_version=frozen_version
        ) == ()
    assert critic_schema_retry_feedback(ContractError("unrelated runtime error")) == ()
    assert parse_critic_verdict(
        _verdict(list(MISSION_CRITERIA)), expected_criteria=MISSION_CRITERIA
    ).passed


@pytest.mark.parametrize("cold_restart", [False, True])
def test_critic_schema_retry_is_independent_and_both_services_are_charged(
    tmp_path, monkeypatch, cold_restart
):
    seen_packages: list[dict] = []

    def malformed(request):
        package = package_of(request)
        seen_packages.append(package)
        assert "feedback" not in package
        assert package["mission_success_criteria"] == list(MISSION_CRITERIA)
        assert package["task_contract"]["success_criteria"] == [TASK_CRITERION]
        return _verdict([TASK_CRITERION, *MISSION_CRITERIA])

    def corrected(request):
        package = package_of(request)
        seen_packages.append(package)
        assert package["feedback"] == SCHEMA_FEEDBACK
        assert package["mission_success_criteria"] == list(MISSION_CRITERIA)
        return _verdict(list(MISSION_CRITERIA))

    task = node(
        "A",
        tokens=120_000,
        success_criteria=[TASK_CRITERION],
        verification_policy=["format_check", "rule_check", "critic_review"],
        outputs=list(FILES),
    )
    provider = RoleScriptedProvider({
        "planner": [graph_proposal_step([task])],
        "worker": [
            *[("workspace_write_file", {"path": path, "content": FILE_CONTENT})
              for path in FILES],
            envelope_step(
                summary="wrote fixture files", artifacts=FILES, claims=["fixture files exist"]
            ),
        ],
        "critic": [
            ("workspace_read_file", {"path": "task.md"}), malformed,
            ("workspace_read_file", {"path": "task.md"}), corrected,
        ],
    }, usage_tokens=100)
    config = OrchestratorConfig(
        evidence_root=tmp_path / "runtime",
        max_concurrency=1,
        candidates_per_task=1,
        dynamic_graph=False,
    )
    mission_spec = spec(
        "critic-schema-cold" if cold_restart else "critic-schema-hot",
        success_criteria=MISSION_CRITERIA,
        budget=Budget(max_tokens=240_000, max_attempts=8),
    )

    async def assert_finished(orch, mission_id):
        await asyncio.wait_for(orch.run(), 30)
        assert orch.store.get_mission(mission_id).status is MissionStatus.COMPLETED
        [completed] = orch.store.list_tasks(mission_id)
        assert completed.status is TaskStatus.COMPLETED
        [attempt] = orch.store.list_attempts(completed.id)
        first = orch.store.get_intent_for_subject(f"{attempt.id}:critic:1")
        second = orch.store.get_intent_for_subject(f"{attempt.id}:critic:2")
        assert first is not None and second is not None
        assert first.state == "FAILED" and second.state == "SETTLED"
        assert first.agent_id != second.agent_id
        assert first.expected_turn_id != second.expected_turn_id
        assert first.config["prompt_version"] == second.config["prompt_version"] == "critic-v3"
        assert provider.by_role["critic"] == 4  # one read and one verdict per service
        assert len(seen_packages) == 2
        rows = [
            row for row in orch.store.list_verifications(completed.accepted_result_id)
            if row["layer"] == "critic_review"
        ]
        assert len(rows) == 1 and rows[0]["status"] == "PASS"
        assert rows[0]["detail"]["critic_intent_id"] == second.intent_id
        assert rows[0]["detail"]["verifier_version"] == "critic-v3"
        for intent in (first, second):
            row = orch.store.connection.execute(
                "SELECT state, settled_tokens FROM budget_reservations WHERE subject_id=?",
                (intent.subject_id,),
            ).fetchone()
            assert row is not None and row["state"] == "SETTLED"
            assert row["settled_tokens"] == 300  # two real fixture Provider calls

    async def exercise():
        if cold_restart:
            async with Orchestrator(config, provider) as first:
                mission = await first.submit_mission(mission_spec)
                settle = first._settle_intent

                def crash_after_first_failed(intent, state):
                    settle(intent, state)
                    if intent.kind == "critic" and state == "FAILED":
                        raise InjectedCrash("critic-schema-first-settled")

                with monkeypatch.context() as patch:
                    patch.setattr(first, "_settle_intent", crash_after_first_failed)
                    with pytest.raises(InjectedCrash, match="critic-schema-first-settled"):
                        await asyncio.wait_for(first.run(), 30)
                assert provider.by_role["critic"] == 2
                [work] = first.store.list_tasks(mission.id)
                [attempt] = first.store.list_attempts(work.id)
                old_intent = first.store.get_intent_for_subject(f"{attempt.id}:critic:1")
                assert old_intent.state == "FAILED"
                frozen = old_intent.to_json()
            async with Orchestrator(config, provider) as reopened:
                assert reopened.store.get_intent(old_intent.intent_id).to_json() == frozen
                await assert_finished(reopened, mission.id)
        else:
            async with Orchestrator(config, provider) as orch:
                mission = await orch.submit_mission(mission_spec)
                await assert_finished(orch, mission.id)

    asyncio.run(exercise())
