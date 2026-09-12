"""Default runtime system allowance: real graph, real SDK synthesis and Critic.

Oracle: planning protects Mission/global money without a placeholder Work Task;
graph insertion transfers that protection with zero ancestor delta; actual calls
consume the same hold, and terminal release never forgives transferred usage.
"""

import asyncio
import json

import pytest
from fixtures_provider import RoleScriptedProvider
from graph_helpers7 import node, spec

from agent_orchestrator.governance.budgets import BudgetError
from agent_orchestrator.graph.task_graph import TaskGraphProposal
from agent_orchestrator.orchestrator.event_handler import Orchestrator
from agent_orchestrator.runtime.assembly import OrchestratorConfig
from agent_orchestrator.storage.store import Store


def test_default_mission_pool_graph_transfer_is_ancestor_neutral_and_cancel_releases(tmp_path):
    async def exercise():
        async with Orchestrator(
            OrchestratorConfig(evidence_root=tmp_path), RoleScriptedProvider({})
        ) as orch:
            request = spec(
                conflict_reserve_tokens=12000,
                synthesis={
                    "goal": "Combine the actual file",
                    "success_criteria": ["file:s.md"],
                    "outputs": ["s.md"],
                    "verification_policy": ["format_check", "rule_check"],
                    "budget": {"max_tokens": 20000, "max_attempts": 2},
                },
            )
            mission = await orch.submit_mission(request)
            pools = orch.store.connection.execute(
                "SELECT * FROM mission_system_tail_pools WHERE mission_id=?", (mission.id,)
            ).fetchall()
            assert {r["purpose"] for r in pools} == {"conflict", "synthesis"}
            assert orch.store.list_tasks(mission.id) == []
            assert all(
                json.loads(r["binding_json"])["worker"]["profile_fingerprint"] for r in pools
            )
            before = orch.commit.ledger.account("budget:" + mission.id)
            assert before.reserved_tokens == 32000 and before.reserved_attempts == 4
            assert (await orch.submit_mission(request)).id == mission.id
            planning = orch.commit.begin_planning(mission.id)
            tasks, _ = orch.commit.commit_task_graph(
                mission.id,
                TaskGraphProposal.from_json({"tasks": [node("A")]}),
                base_version=planning.version,
                source={"planner": "legal graph fixture"},
            )
            after = orch.commit.ledger.account("budget:" + mission.id)
            assert (after.reserved_tokens, after.reserved_cost_micros, after.reserved_attempts) == (
                before.reserved_tokens,
                before.reserved_cost_micros,
                before.reserved_attempts,
            )
            assert orch.commit.ledger.account("budget:" + tasks[0].id).reserved_tokens == 0
            assert orch.commit.ledger.account("budget:" + tasks[-1].id).reserved_tokens == 20000
            orch.commit.cancel_mission(mission.id)
            final = orch.commit.ledger.account("budget:" + mission.id)
            assert final.reserved_tokens == final.reserved_attempts == 0
            assert all(
                r[0] == "RELEASED"
                for r in orch.store.connection.execute(
                    "SELECT state FROM mission_system_tail_pools WHERE mission_id=?", (mission.id,)
                )
            )

    asyncio.run(exercise())


def test_default_actual_synthesis_and_critic_consume_original_system_hold(tmp_path, monkeypatch):
    from test_p33_source_lineage_runtime import (
        test_real_synthesis_inherits_upstream_sources_and_context_excludes_stale as actual,
    )

    # Existing real Worker/tool -> synthesis/tool -> actual Critic/read -> accept
    # supplies authentic verification records; this oracle adds accounting checks.
    actual(tmp_path, "revoke", monkeypatch)
    store = Store.open(tmp_path / "orchestrator.db")
    try:
        row = store.connection.execute("SELECT * FROM mission_system_tail_tasks").fetchone()
        assert row is not None
        task = store.get_task(row["task_id"])
        assert task.kind == "synthesis" and str(task.status) == "COMPLETED"
        transfers = store.connection.execute(
            "SELECT transfer_id FROM budget_tail_transfers WHERE hold_id=?", (row["hold_id"],)
        ).fetchall()
        subjects = [r[0] for r in transfers]
        assert len(subjects) == 2
        assert {store.get_intent_for_subject(s).kind for s in subjects} == {"attempt", "critic"}
        for subject in subjects:
            intent = store.get_intent_for_subject(subject)
            assert intent.agent_id and intent.expected_turn_id
            reservation = store.connection.execute(
                "SELECT * FROM budget_reservations WHERE subject_id=?", (subject,)
            ).fetchone()
            assert reservation["state"] == "SETTLED"
        assert (
            store.connection.execute(
                "SELECT state FROM budget_tail_holds WHERE hold_id=?", (row["hold_id"],)
            ).fetchone()[0]
            == "RELEASED"
        )
        assert (
            store.connection.execute(
                "SELECT COUNT(*) FROM budget_tail_holds WHERE hold_id LIKE ?",
                ("first-critic:" + task.id + "%",),
            ).fetchone()[0]
            == 0
        )
    finally:
        store.close()


def test_default_actual_conflict_and_critic_use_mission_pool_then_human_releases(
    tmp_path, monkeypatch
):
    from test_p33_doc_arbitration_runtime import (
        test_document_conflict_first_human_wait_reopens_and_rules_without_upgrading as actual,
    )

    actual(tmp_path, "contextual", monkeypatch)
    store = Store.open(tmp_path / "orchestrator.db")
    try:
        row = store.connection.execute("SELECT * FROM mission_system_tail_tasks").fetchone()
        assert row is not None
        task = store.get_task(row["task_id"])
        assert task.kind == "conflict" and str(task.status) == "CANCELLED"
        transfers = store.connection.execute(
            "SELECT transfer_id FROM budget_tail_transfers WHERE hold_id=?", (row["hold_id"],)
        ).fetchall()
        assert len(transfers) == 2
        assert {store.get_intent_for_subject(r[0]).kind for r in transfers} == {"attempt", "critic"}
        assert (
            store.connection.execute(
                "SELECT state FROM budget_tail_holds WHERE hold_id=?", (row["hold_id"],)
            ).fetchone()[0]
            == "RELEASED"
        )
    finally:
        store.close()


@pytest.mark.parametrize("changed", [False, True])
def test_pool_cold_graph_transfer_binds_original_runtime_not_current_price(tmp_path, changed):
    from dataclasses import replace

    async def exercise():
        config = OrchestratorConfig(evidence_root=tmp_path)
        request = spec(
            synthesis={
                "goal": "Combine files",
                "success_criteria": ["file:s.md"],
                "outputs": ["s.md"],
                "verification_policy": ["format_check", "rule_check"],
                "budget": {"max_tokens": 20000, "max_attempts": 2},
            }
        )
        async with Orchestrator(config, RoleScriptedProvider({})) as first:
            mission = await first.submit_mission(request)
            binding = first.store.connection.execute(
                "SELECT binding_json FROM mission_system_tail_pools WHERE mission_id=?",
                (mission.id,),
            ).fetchone()[0]
        if changed:
            config = replace(config, attempt_reserve_tokens=config.attempt_reserve_tokens + 1)
        async with Orchestrator(config, RoleScriptedProvider({})) as second:
            planning = second.commit.begin_planning(mission.id)
            before = second.commit.ledger.account("budget:" + mission.id).to_json()

            def graph():
                return second.commit.commit_task_graph(
                    mission.id,
                    TaskGraphProposal.from_json({"tasks": [node("A")]}),
                    base_version=planning.version,
                    source={"planner": "legal graph fixture"},
                )

            if changed:
                with pytest.raises(BudgetError, match="routing/price"):
                    graph()
                assert second.store.list_tasks(mission.id) == []
                assert second.commit.ledger.account("budget:" + mission.id).to_json() == before
            else:
                tasks, _ = graph()
                assert second.commit.system_task_hold(tasks[-1].id) is not None
            assert (
                second.store.connection.execute(
                    "SELECT binding_json FROM mission_system_tail_pools WHERE mission_id=?",
                    (mission.id,),
                ).fetchone()[0]
                == binding
            )

    asyncio.run(exercise())


def test_priced_system_tail_protects_actual_double_rounding_and_output_retry(tmp_path):
    from dataclasses import replace

    from fixtures_provider import critic_step, envelope_step
    from test_provider_budget_guard import Counter, grants

    from agent_orchestrator.contracts import Budget, TaskStatus
    from agent_orchestrator.orchestrator.commit_service import Reservation
    from agent_orchestrator.runtime.agent_worker import user_message_json
    from agent_orchestrator.runtime.assembly import PriceTable
    from simple_harness.agents import AgentConfig, AgentLimits

    class ActualEmptyRetry(RoleScriptedProvider):
        async def invoke(self, request, *, cancel):
            response = await super().invoke(request, cancel=cancel)
            if response.message.content == "" and not response.tool_calls:
                response = replace(response, finish_reason="length")
            return response

    provider = ActualEmptyRetry(
        {
            "worker": [
                ("workspace_write_file", {"path": "a.md", "content": "input"}),
                envelope_step(summary="input", artifacts=["a.md"], claims=["input"]),
            ],
            "synthesizer": [
                "",  # actual length response; SDK retries within the same durable call cap
                ("workspace_read_file", {"path": "a.md"}),
                ("workspace_write_file", {"path": "s.md", "content": "combined"}),
                envelope_step(summary="combined", artifacts=["s.md"], claims=["combined"]),
            ],
            "critic": [
                ("workspace_read_file", {"path": "s.md"}),
                critic_step(verdict="PASS", criteria_met=True),
            ],
        }
    )

    async def exercise():
        config = OrchestratorConfig(
            evidence_root=tmp_path,
            max_concurrency=1,
            max_model_calls_per_turn=10,
            default_max_output_tokens=1000,
            max_output_tokens_ceiling=2000,
            empty_response_retries=1,
            price_table=PriceTable("low-exact", 1, 1),
        )
        async with Orchestrator(config, provider, provider_token_estimator=Counter(1000)) as orch:
            mission = await orch.submit_mission(
                spec(
                    budget=Budget(max_tokens=200000, max_cost_micros=100, max_attempts=12),
                    synthesis={
                        "goal": "Combine input",
                        "success_criteria": ["file:s.md"],
                        "outputs": ["s.md"],
                        "verification_policy": ["format_check", "rule_check", "critic_review"],
                        "budget": {"max_tokens": 40000, "max_cost_micros": 68, "max_attempts": 1},
                    },
                )
            )
            pool = orch.store.connection.execute(
                "SELECT request_json FROM mission_system_tail_pools WHERE mission_id=?",
                (mission.id,),
            ).fetchone()
            # 40k at rate1 + rounding of 10 Worker + 2*12 Critic calls = 68.
            # Retry is included in 10, not multiplied by (1 + retry limit).
            assert json.loads(pool[0])["reserve"]["cost_micros"] == 68
            planning = orch.commit.begin_planning(mission.id)
            tasks, _ = orch.commit.commit_task_graph(
                mission.id,
                TaskGraphProposal.from_json(
                    {
                        "tasks": [
                            node(
                                "A",
                                budget={
                                    "max_tokens": 20000,
                                    "max_cost_micros": 20,
                                    "max_attempts": 3,
                                },
                            )
                        ]
                    }
                ),
                base_version=planning.version,
                source={"planner": "legal graph fixture"},
            )

            async def drive(original):
                task = orch.store.get_task(original.id)
                assert await orch._next_attempt(orch.store.get_mission(mission.id), task, [])
                [attempt] = orch.store.list_attempts(task.id)
                intent = orch.store.get_intent_for_subject(attempt.id)
                assert await orch._dispatch(intent)
                intent = orch.store.get_intent(intent.intent_id)

                async def completed():
                    while True:
                        result = await orch.bridge_for(intent).result(
                            agent_id=intent.agent_id, turn_id=intent.expected_turn_id
                        )
                        if result is not None:
                            return result
                        await asyncio.sleep(0.002)

                await orch._collect_attempt(intent, await asyncio.wait_for(completed(), 10))
                stored = orch.store.find_result_for_attempt(attempt.id)
                assert stored is not None, orch.progress_log
                assert await orch._verify(stored.envelope.id)
                assert orch.store.get_task(task.id).status is TaskStatus.COMPLETED
                return attempt

            await drive(tasks[0])
            # Another genuine public service reservation owns all unprotected
            # Mission money. The tested C/Critic cannot borrow it via guard.grow.
            account = orch.commit.ledger.account("budget:" + mission.id)
            message = user_message_json("Independent pending management work")
            orch.commit.create_service_intent(
                kind="manager",
                subject_id=mission.id + ":competing-manager",
                mission_id=mission.id,
                account_id="budget:" + mission.id,
                creation_key="competing",
                input_id="competing",
                input_hash="fixture",
                config={
                    "agent_config": AgentConfig(
                        name="manager",
                        model_profile_ref="default",
                        instructions="Review",
                        limits=AgentLimits(max_model_calls_per_turn=10),
                    ).to_json(),
                    "message": message,
                },
                reservation=Reservation(2000, account.remaining_cost_micros()),
            )
            assert orch.commit.ledger.account("budget:" + mission.id).remaining_cost_micros() == 0
            attempt = await drive(tasks[-1])
            actual = [g for g in grants(orch.commit) if g["subject_id"].startswith(attempt.id)]
            assert len(actual) == 6  # empty + read/write/envelope + Critic read/verdict
            assert all(g["state"] == "SETTLED" and g["actual_cost_micros"] == 2 for g in actual)
            assert provider.by_role["synthesizer"] == 4 and provider.by_role["critic"] == 2
            assert orch.store.get_mission(mission.id).budget.max_cost_micros == 100
            orch.commit.cancel_mission(mission.id)

    asyncio.run(exercise())
