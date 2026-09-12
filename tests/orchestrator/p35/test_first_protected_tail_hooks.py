# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""Production FIRST tails: Commit contracts and actual Orchestrator/SDK controls."""

import pytest
from graph_helpers7 import graph_service, node

from agent_orchestrator.contracts import ids
from agent_orchestrator.governance.budgets import BudgetError, BudgetExhausted
from agent_orchestrator.governance.tail_budget import TailReserve
from agent_orchestrator.orchestrator.commit_service import Reservation, task_account


@pytest.fixture
def harness(tmp_path):
    original, mission, tasks = graph_service(tmp_path, nodes=[node("A"), node("B", ["A"])])
    commit = original
    try:
        yield commit, mission, tasks
    finally:
        commit.store.close()


def attempt(commit, task):
    return commit.create_attempt(
        task.id,
        role="worker",
        model="fixture",
        prompt_version="worker-v2",
        context_version="ctx",
        reservation=Reservation(4000, 0),
        intent_config={},
        input_hash="fixture-worker-input",
    )[0]


def balances(commit, task):
    return [
        (a.reserved_tokens, a.reserved_cost_micros, a.attempts_created, a.reserved_attempts)
        for a in commit.ledger._chain(task_account(task.id))
    ]


def test_first_critic_hooks_use_actual_identity_routed_amount_and_same_revision(harness):
    commit, mission, tasks = harness
    task = tasks["A"]
    with commit.store.transaction():
        revision = commit.protected_tail_revision(task.id)
        actual_id = ids.attempt_id(task.id, 1)
        hold = commit.reserve_critic_tail(
            attempt_id=actual_id,
            task_id=task.id,
            reserve=TailReserve(6000, 180),
            semantic_revision=revision,
        )
        worker = attempt(commit, task)
        assert worker.id == actual_id
        # A status/version transition is not a change of the original contract.
        assert commit.protected_tail_revision(task.id) == revision
        before = balances(commit, task)
        subject = f"{worker.id}:critic:1"
        args = dict(
            attempt_id=worker.id,
            task_id=task.id,
            subject_id=subject,
            account_id=task_account(task.id),
            semantic_revision=revision,
        )
        with pytest.raises(BudgetExhausted, match="cost_micros"):
            commit.consume_critic_tail(**args, reservation=Reservation(2000, 181))
        with pytest.raises(BudgetError, match="revision"):
            commit.consume_critic_tail(
                **{**args, "semantic_revision": "stale"}, reservation=Reservation(2000, 120)
            )
        with pytest.raises(BudgetError, match="account"):
            commit.consume_critic_tail(
                **{**args, "account_id": task_account(tasks["B"].id)},
                reservation=Reservation(2000, 120),
            )
        with pytest.raises(BudgetError, match="subject"):
            commit.consume_critic_tail(
                **{**args, "subject_id": f"{ids.attempt_id(task.id, 2)}:critic:1"},
                reservation=Reservation(2000, 120),
            )
        assert balances(commit, task) == before
        assert commit.ledger.reservation(subject) is None
        receipt = commit.consume_critic_tail(**args, reservation=Reservation(2000, 120))
        assert balances(commit, task) == before
        intent = commit.create_service_intent(
            kind="critic",
            subject_id=subject,
            task_id=task.id,
            attempt_id=worker.id,
            mission_id=mission.id,
            account_id=task_account(task.id),
            creation_key=subject,
            input_id="attempt-input",
            input_hash="actual-critic-input",
            config={"actual_profile": "routed-profile-b"},
            reservation=Reservation(2000, 120),
        )
        assert (
            intent.subject_id == subject and intent.config["actual_profile"] == "routed-profile-b"
        )
        assert balances(commit, task) == before
        assert commit.consume_critic_tail(**args, reservation=Reservation(2000, 120)) == receipt
        assert commit.ledger.reservation(hold["subject_id"])["reserved_cost_micros"] == 60
        assert commit.ledger.reservation(subject)["reserved_cost_micros"] == 120


def test_first_tail_and_attempt_rollback_together_and_terminal_release_is_scoped(harness):
    commit, mission, tasks = harness
    task = tasks["A"]
    original = balances(commit, task)
    revision = commit.protected_tail_revision(task.id)
    actual_id = ids.attempt_id(task.id, 1)
    with pytest.raises(RuntimeError, match="crash-before-commit"):
        with commit.store.transaction():
            commit.reserve_critic_tail(
                attempt_id=actual_id,
                task_id=task.id,
                reserve=TailReserve(6000, 180),
                semantic_revision=revision,
            )
            assert attempt(commit, task).id == actual_id
            raise RuntimeError("crash-before-commit")
    assert balances(commit, task) == original
    assert commit.store.get_attempt(actual_id) is None
    assert commit.protected_tail_hold(commit.critic_tail_id(actual_id)) is None
    with commit.store.transaction():
        commit.reserve_critic_tail(
            attempt_id=actual_id,
            task_id=task.id,
            reserve=TailReserve(6000, 180),
            semantic_revision=revision,
        )
        attempt(commit, task)
        before = balances(commit, task)
        assert commit.release_terminal_tail_holds(mission_id=mission.id, task_id=task.id) == []
        assert balances(commit, task) == before
    commit.cancel_mission(mission.id)
    with commit.store.transaction():
        # Public production cancel already performed the unused-tail release.
        assert commit.release_terminal_tail_holds(mission_id=mission.id, task_id=task.id) == []
        hold = commit.protected_tail_hold(commit.critic_tail_id(actual_id))
        assert hold["state"] == "RELEASED"
        assert commit.ledger.reservation(hold["subject_id"])["state"] == "SETTLED"
        assert commit.ledger.account(task_account(task.id)).attempts_created == 1


def test_system_hook_rejects_imaginary_task_or_attempt_and_normal_work_task(harness):
    commit, _, tasks = harness
    task = tasks["A"]
    with commit.store.transaction():
        revision = commit.protected_tail_revision(task.id)
        before = balances(commit, task)
        with pytest.raises(BudgetError, match="actual Attempt"):
            commit.reserve_critic_tail(
                attempt_id="invented-attempt",
                task_id=task.id,
                reserve=TailReserve(1000, 0),
                semantic_revision=revision,
            )
        with pytest.raises(BudgetError, match="system Task"):
            commit.reserve_system_tail(
                task_id=task.id,
                reserve=TailReserve(1000, 0, attempts=1),
                semantic_revision=revision,
            )
        with pytest.raises(BudgetError, match="does not exist"):
            commit.reserve_system_tail(
                task_id="imaginary-system",
                reserve=TailReserve(1000, 0, attempts=1),
                semantic_revision="invented",
            )
        assert balances(commit, task) == before


def test_production_first_hooks_run_actual_orchestrator_worker_tool_and_critic(tmp_path):
    from test_provider_budget_recovery import (
        test_real_orchestrator_planner_worker_critic_and_whole_account_chain as actual_control,
    )

    from agent_orchestrator.storage.store import Store

    # No subclass or monkeypatch: actual production default drives the complete
    # Planner -> Worker file write -> Critic file read -> formal acceptance chain.
    actual_control(tmp_path)
    store = Store.open(tmp_path / "whole" / "orchestrator.db")
    try:
        holds = store.connection.execute(
            "SELECT * FROM budget_tail_holds WHERE hold_id LIKE 'first-critic:%'"
        ).fetchall()
        assert len(holds) == 1 and holds[0]["state"] == "RELEASED"
        transfers = store.connection.execute(
            "SELECT * FROM budget_tail_transfers WHERE hold_id=?", (holds[0]["hold_id"],)
        ).fetchall()
        assert len(transfers) == 1
        subject_id = transfers[0]["transfer_id"]
        intent = store.get_intent_for_subject(subject_id)
        assert intent is not None and intent.kind == "critic" and intent.agent_id
        actual = store.connection.execute(
            "SELECT * FROM provider_token_grants WHERE subject_id=?", (subject_id,)
        ).fetchall()
        assert actual and all(r["state"] == "SETTLED" and r["actual_tokens"] > 0 for r in actual)
    finally:
        store.close()


@pytest.mark.parametrize("stop", ["known_failure", "cancel_unknown"])
def test_production_first_unused_tail_release_preserves_actual_call_accounting(tmp_path, stop):
    import asyncio

    from test_provider_budget_guard import ActualProvider, grants
    from test_tail_and_priced_budget import priced_runtime

    from agent_orchestrator.runtime.agent_worker import AgentBridge, user_message_json
    from simple_harness.agents import AgentConfig

    async def exercise():
        provider = ActualProvider(blocked=stop == "cancel_unknown")
        if stop == "known_failure":
            provider.script[0] = ""  # real length/empty failure with actual priced usage
        async with priced_runtime(tmp_path, provider=provider) as (
            commit,
            mission,
            task,
            guard,
            _,
            runtime,
        ):
            agent = await runtime.create(
                AgentConfig(
                    name=stop, instructions="Answer briefly.", model_profile_ref="agent.general"
                ),
                creation_key=stop,
            )
            worker, intent = commit.create_attempt(
                task.id,
                role="worker",
                model="agent-model",
                prompt_version="worker-v2",
                context_version="ctx",
                reservation=Reservation(4000, 0),
                critic_tail=Reservation(6000, 600),
                intent_config={
                    "agent_config": agent.config.to_json(),
                    "message": user_message_json("request"),
                    "provider_admission_fingerprint": guard.fingerprint,
                },
                input_hash="actual-request",
            )
            commit.claim_intent(intent.intent_id, owner="test-owner", lease_seconds=60)
            commit.record_agent_created(
                intent.intent_id,
                agent_id=agent.agent_id,
                expected_turn_id=agent.turn_id_for(intent.input_id),
            )
            receipt = await agent.submit("request", input_id=intent.input_id)
            commit.record_submitted(
                intent.intent_id, receipt={"turn_id": receipt.turn_id, "seq": receipt.seq}
            )
            await asyncio.wait_for(provider.entered.wait(), 5)
            bridge = AgentBridge(runtime, unpriced=False)
            if stop == "cancel_unknown":
                commit.cancel_mission(mission.id)
                guard.recover(runtime.uow)
                assert grants(commit)[0]["state"] == "UNKNOWN"
                assert bridge.usage_facts(agent_id=agent.agent_id) == []
                with commit.store.transaction():
                    with pytest.raises(BudgetError, match="unknown"):
                        commit.ledger.settle(subject_id=worker.id)
                assert commit.ledger.reservation(worker.id)["reserved_cost_micros"] == 2100
                provider.allow.set()
                await agent.wait_turn(receipt.turn_id, timeout=5)
                facts = bridge.usage_facts(agent_id=agent.agent_id)
                commit.import_usage(worker.id, mission.id, facts)
                commit.settle_subject(worker.id, mission.id, task_id=task.id)
            else:
                failed = await agent.wait_turn(receipt.turn_id, timeout=5)
                assert str(failed.state) == "failed"
                commit.import_usage(
                    worker.id, mission.id, bridge.usage_facts(agent_id=agent.agent_id)
                )
                commit.mark_attempt_lost(worker.id, reason="actual_sdk_empty_response")
            hold = commit.protected_tail_hold(commit.critic_tail_id(worker.id))
            assert hold["state"] == "RELEASED"
            assert commit.ledger.reservation(hold["subject_id"])["state"] == "SETTLED"
            assert commit.ledger.reservation(worker.id)["settled_cost_micros"] == 200
            assert provider.calls == 1
            assert not commit.store.list_mission_claims(mission.id)

    asyncio.run(exercise())
