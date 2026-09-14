"""Mission-funded final review must not borrow a Task Critic's protected tail."""

import asyncio

from graph_helpers7 import node, spec
from test_provider_budget_guard import Counter, grants

from agent_orchestrator.contracts import Budget, MissionStatus
from agent_orchestrator.orchestrator.commit_service import mission_account
from agent_orchestrator.orchestrator.event_handler import Orchestrator
from agent_orchestrator.runtime.assembly import OrchestratorConfig
from agent_orchestrator.runtime.model_router import RuntimeProfile
from agent_orchestrator.testing.fixtures import (
    MODEL,
    RoleScriptedProvider,
    critic_step,
    envelope_step,
    graph_proposal_step,
)


def test_final_judge_grows_own_mission_reservation_before_real_sdk_handoff(tmp_path):
    async def run():
        provider = RoleScriptedProvider({
            "planner": [graph_proposal_step([node("A", tokens=120_000)])],
            "worker": [
                ("workspace_write_file", {"path": "a.md", "content": "review me\n"}),
                envelope_step(summary="written", artifacts=["a.md"], claims=["file exists"]),
            ],
            "critic": [
                ("workspace_read_file", {"path": "a.md"}),
                critic_step(verdict="PASS", criteria_met=True),
            ],
        })
        profile = RuntimeProfile(
            "default", provider, MODEL, max_concurrent_model_calls=1,
            default_max_output_tokens=8192, max_output_tokens_ceiling=8192,
        )
        config = OrchestratorConfig(
            evidence_root=tmp_path, max_concurrency=1, candidates_per_task=1,
            critic_reserve_tokens=6000,
        )
        async with Orchestrator(
            config, profiles={"default": profile}, provider_token_estimator=Counter(1702),
        ) as orch:
            mission = await orch.submit_mission(spec(
                "final-judge-growth", success_criteria=("The delivered file is readable",),
                budget=Budget(max_tokens=300_000, max_attempts=12),
            ))
            await asyncio.wait_for(orch.run(), 15)
            final = orch.store.get_mission(mission.id)
            assert final.status is MissionStatus.COMPLETED, orch.progress_log
            assert final.budget == mission.budget
            assert provider.by_role["critic"] == 2
            criteria = final.final_report["success_criteria"]
            assert criteria[0]["source"] == "independent" and criteria[0]["met"]
            intent = orch.store.get_intent_for_subject(f"{mission.id}:judge:1")
            reservation = orch.commit.ledger.reservation(intent.subject_id)
            assert reservation["account_id"] == mission_account(mission.id)
            assert reservation["reserved_tokens"] > 6000
            assert reservation["state"] == "SETTLED"
            rows = [r for r in grants(orch.commit) if r["subject_id"] == intent.subject_id]
            assert len(rows) == 2 and all(r["state"] == "SETTLED" for r in rows)
            assert not orch.store.connection.execute(
                "SELECT 1 FROM budget_tail_transfers WHERE transfer_id=?", (intent.subject_id,),
            ).fetchall()

    asyncio.run(run())


def test_final_judge_exception_rejects_foreign_or_malformed_authority(tmp_path):
    import pytest
    from graph_helpers7 import graph_service

    from agent_orchestrator.governance.budgets import BudgetError
    from agent_orchestrator.orchestrator.commit_service import Reservation, task_account

    commit, mission, tasks = graph_service(tmp_path, nodes=[node("A")])
    task = tasks["A"]
    try:
        cases = ("foreign_account", "task_id", "wrong_subject", "no_view")
        for ordinal, case in enumerate(cases, 1):
            subject = f"{mission.id}:judge:{ordinal}"
            if case == "wrong_subject":
                subject = f"{mission.id}:critic:{ordinal}"
            commit.create_service_intent(
                kind="critic", subject_id=subject, mission_id=mission.id,
                account_id=(task_account(task.id) if case == "foreign_account"
                            else mission_account(mission.id)),
                creation_key=subject, input_id="input", input_hash="controlled",
                config={
                    "attempt_id": None if case == "no_view" else f"{mission.id}-judge-view",
                    "task_id": task.id if case == "task_id" else None,
                }, reservation=Reservation(6000, 0),
            )
            with commit.store.transaction():
                before = dict(commit.ledger.reservation(subject))
                with pytest.raises(BudgetError, match="actual Attempt"):
                    commit.grow_system_worker_allowance(subject, tokens=9894, cost_micros=0)
                assert dict(commit.ledger.reservation(subject)) == before
    finally:
        commit.store.close()
