# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""Real SDK requests and full verification; deterministic in-process Provider only.

The P34 failed pair is never opened or resumed. S keeps 240K and Mission 2M.
The same tests were first run against unchanged source to demonstrate the defects.
"""

import asyncio
import json
from dataclasses import replace

import pytest
from fixtures_provider import (
    RoleScriptedProvider,
    critic_step,
    envelope_step,
    graph_proposal_step,
    package_of,
    role_of,
)
from graph_helpers7 import node, spec
from test_provider_budget_guard import ActualProvider, Counter, grants
from test_provider_budget_recovery import until
from test_system_worker_budget_growth import _rows, _runtime, _worker

from agent_orchestrator.contracts import Budget, MissionStatus, TaskStatus
from agent_orchestrator.governance.budgets import BudgetError
from agent_orchestrator.orchestrator.commit_service import Reservation, task_account
from agent_orchestrator.orchestrator.event_handler import Orchestrator
from agent_orchestrator.runtime.agent_worker import AgentBridge, user_message_json
from agent_orchestrator.runtime.assembly import OrchestratorConfig
from agent_orchestrator.runtime.model_router import RuntimeProfile
from simple_harness.agents import AgentConfig
from simple_harness.agents.context.budget import ContextPolicy
from simple_harness.contracts import RunId
from simple_harness.execution.provider_admission import ProviderAdmissionDenied
from simple_harness.providers import (
    ProviderReconciliationObservation,
    ProviderReconciliationState,
    ProviderUsage,
)
from simple_harness.providers.errors import ProviderRequestRejectedError


def _scenario(tmp_path, mode):
    seen = []

    def verdict(request):
        seen.append(package_of(request))
        return critic_step(verdict="PASS", criteria_met=True)(request)

    def malformed(request):
        seen.append(package_of(request))
        return '<critic_verdict>{"verdict":"PASS","findings":[],' \
            '"mission_criteria":[]}</critic_verdict>'

    def rejected(request):
        seen.append(package_of(request))
        # A known transport/protocol rejection is NOT a local admission denial.
        raise ProviderRequestRejectedError(public_message="fixture rejection", retryable=True)

    read = ("workspace_read_file", {"path": "FINAL.md"})
    critic_script = {
        "growth": [read, verdict],
        "budget": [read, ("workspace_list", {})] * 5 + [verdict],
        "input_cap": [verdict],
        "schema": [malformed, verdict],
        "transport": [rejected, verdict],
    }[mode]
    worker_script = [
        ("workspace_write_file", {"path": "a.md", "content": "upstream\n"}),
        envelope_step(summary="upstream", artifacts=["a.md"], claims=["a.md exists"]),
    ]
    synthesis_script = [
        ("workspace_write_file", {"path": "FINAL.md", "content": "reviewed summary\n"}),
        envelope_step(summary="synthesis", artifacts=["FINAL.md"], claims=["FINAL.md exists"]),
    ]

    class Provider(RoleScriptedProvider):
        async def invoke(self, request, *, cancel):
            response = await super().invoke(request, cancel=cancel)
            if role_of(request) == "critic" and mode in {"growth", "budget"}:
                amount = 14_000 if mode == "growth" else 24_000
                return replace(response, usage=ProviderUsage(
                    input_tokens=amount, output_tokens=1000, total_tokens=amount + 1000,
                ))
            return response

    class Estimator(Counter):
        allow_input = False

        def estimate_input_tokens(self, request):
            if role_of(request) != "critic":
                self.tokens = 100
            elif mode == "input_cap" and not self.allow_input:
                self.tokens = 40_000
            elif mode in {"growth", "budget"}:
                self.tokens = 25_000
            else:
                self.tokens = 100
            return super().estimate_input_tokens(request)

    provider = Provider({
        "planner": [graph_proposal_step([node("A", tokens=240_000)])],
        "worker": worker_script,
        # Extra scripts let the OLD code expose an actual redo instead of failing
        # only because the fixture ran out of replies.
        "synthesizer": synthesis_script * 2,
        "critic": critic_script * 4,
    })
    config = OrchestratorConfig(
        evidence_root=tmp_path / "runtime", max_concurrency=1, candidates_per_task=1,
        dynamic_graph=False, knowledge_sharing=False,
        attempt_reserve_tokens=60_000, critic_reserve_tokens=30_000,
        default_max_output_tokens=8192, max_output_tokens_ceiling=8192,
    )
    mission_spec = spec(
        "critic-hold-" + mode, success_criteria=("file:FINAL.md",),
        budget=Budget(max_tokens=2_000_000, max_attempts=24),
        synthesis={
            "goal": "Combine the accepted upstream file",
            "success_criteria": ["file:FINAL.md"], "outputs": ["FINAL.md"],
            "verification_policy": ["format_check", "rule_check", "critic_review"],
            "budget": {"max_tokens": 240_000, "max_attempts": 2},
        },
    )
    return config, mission_spec, provider, Estimator(100), seen


def test_actual_sdk_critic_grows_original_40960_hold_and_completes_without_worker_redo(tmp_path):
    async def exercise():
        config, mission_spec, provider, estimator, _ = _scenario(tmp_path, "growth")
        async with Orchestrator(
            config, provider, provider_token_estimator=estimator,
            profiles={"default": RuntimeProfile(
                "default", provider, config.model,
                context_policy=ContextPolicy(max_input_tokens=32768, render_slack_tokens=0),
            )},
        ) as orch:
            mission = await orch.submit_mission(mission_spec)
            await asyncio.wait_for(orch.run(), 30)
            assert orch.store.get_mission(mission.id).status is MissionStatus.COMPLETED
            task = next(t for t in orch.store.list_tasks(mission.id) if t.kind == "synthesis")
            assert task.status is TaskStatus.COMPLETED and task.budget.max_tokens == 240_000
            [attempt] = orch.store.list_attempts(task.id)
            subject = f"{attempt.id}:critic:1"
            assert orch.store.get_intent_for_subject(f"{attempt.id}:critic:2") is None
            assert provider.by_role["synthesizer"] == 2 and provider.by_role["critic"] == 2
            layers = orch.store.list_verifications(task.accepted_result_id)
            assert all(next(r for r in layers if r["layer"] == layer)["status"] == "PASS"
                       for layer in ("format_check", "rule_check", "critic_review"))
            row = orch.commit.ledger.reservation(subject)
            # 15K spent + 25K input + 1K prior output + 8192 output ceiling.
            assert row["reserved_tokens"] == 49_192 and row["settled_tokens"] == 30_000
            initial = next(e for e in orch.store.list_events(mission.id)
                           if e.type == "BudgetReserved" and e.payload.get("subject_id") == subject)
            assert initial.payload["tokens"] == 40_960
            receipts = orch.store.connection.execute(
                "SELECT receipt_json FROM commit_receipts WHERE kind='system_critic_growth' "
                "AND subject_id=?", (subject,),
            ).fetchall()
            assert len(receipts) == 1
            assert json.loads(receipts[0][0])["growth"]["tokens"] == 8232
            actual = [g for g in grants(orch.commit) if g["subject_id"] == subject]
            assert len(actual) == 2 and all(g["state"] == "SETTLED" for g in actual)
            assert [g["total_upper"] for g in actual] == [33_192, 34_192]
            assert sum(g["actual_tokens"] for g in actual) == 30_000
            account = orch.commit.ledger.account(task_account(task.id))
            assert account.reserved_tokens == 0 and account.settled_tokens == 30_300
            before = _rows(orch.commit, "budget_accounts", "budget_reservations", "commit_receipts")
            event_count, calls = len(orch.store.list_events(mission.id)), provider.calls
            await asyncio.wait_for(orch.run(), 5)
            assert _rows(orch.commit, *before) == before
            assert provider.calls == calls
            assert len(orch.store.list_events(mission.id)) == event_count

    asyncio.run(exercise())


@pytest.mark.parametrize("mode,reason", [
    ("budget", "budget_exhausted"), ("input_cap", "provider_input_cap_exceeded"),
])
def test_nonretryable_actual_admission_never_spawns_critic2_or_worker_redo(tmp_path, mode, reason):
    async def exercise():
        config, mission_spec, provider, estimator, _ = _scenario(tmp_path, mode)
        async with Orchestrator(
            config, provider, provider_token_estimator=estimator,
            profiles={"default": RuntimeProfile(
                "default", provider, config.model,
                context_policy=ContextPolicy(max_input_tokens=32768, render_slack_tokens=0),
            )},
        ) as orch:
            mission = await orch.submit_mission(mission_spec)
            await asyncio.wait_for(orch.run(), 30)
            task = next(t for t in orch.store.list_tasks(mission.id) if t.kind == "synthesis")
            [attempt] = orch.store.list_attempts(task.id)
            first = orch.store.get_intent_for_subject(f"{attempt.id}:critic:1")
            assert first is not None and first.state == "FAILED"
            assert orch.store.get_intent_for_subject(f"{attempt.id}:critic:2") is None
            assert provider.by_role["synthesizer"] == 2
            # Eight known 25K responses plus the next 41192 bound exceed
            # the unchanged 239700 remaining S budget; no ninth handoff.
            assert provider.by_role.get("critic", 0) == (8 if mode == "budget" else 0)
            finished = orch.store.get_mission(mission.id)
            assert finished.status is MissionStatus.FAILED
            assert finished.stop_reason == ("budget_exhausted" if mode == "budget"
                                            else "runtime_unavailable")
            failure = next(e for e in orch.store.list_events(mission.id)
                           if e.type == "VerificationFailed" and e.attempt_id == attempt.id)
            error = failure.payload["failures"][0]["detail"]["error"]
            assert error["source_kind"] == "provider_admission" and error["retryable"] is False
            assert error["detail"]["reason_code"] == reason
            assert task.accepted_result_id is None
            assert not any(e.type == "VerificationPassed" and e.attempt_id == attempt.id
                           for e in orch.store.list_events(mission.id))
            records = orch.bridge_for(first).runtime.uow.list_provider_invocations(
                RunId(orch.bridge_for(first).runtime.uow.read_agent_binding(first.agent_id).run_id)
            )
            assert records[-1].handoff_attempt == 0  # denied request never reaches Provider
            # The SAME Orchestrator remains usable for an unrelated Mission.
            # Replenish only the deterministic Provider script, never any budget.
            provider.scripts = _scenario(tmp_path, "growth")[2].scripts
            estimator.allow_input = True
            healthy = await orch.submit_mission(replace(
                mission_spec, idempotency_key="unrelated-after-" + mode,
            ))
            await asyncio.wait_for(orch.run(), 30)
            assert orch.store.get_mission(healthy.id).status is MissionStatus.COMPLETED
            assert orch.store.get_mission(mission.id).status is MissionStatus.FAILED

    asyncio.run(exercise())


@pytest.mark.parametrize("mode", ["schema", "transport"])
def test_completed_schema_retry_and_known_transport_rejection_stay_distinct(tmp_path, mode):
    async def exercise():
        config, mission_spec, provider, estimator, seen = _scenario(tmp_path, mode)
        async with Orchestrator(
            config, provider, provider_token_estimator=estimator,
            profiles={"default": RuntimeProfile(
                "default", provider, config.model,
                context_policy=ContextPolicy(max_input_tokens=32768, render_slack_tokens=0),
            )},
        ) as orch:
            mission = await orch.submit_mission(mission_spec)
            await asyncio.wait_for(orch.run(), 30)
            assert orch.store.get_mission(mission.id).status is MissionStatus.COMPLETED
            task = next(t for t in orch.store.list_tasks(mission.id) if t.kind == "synthesis")
            [attempt] = orch.store.list_attempts(task.id)
            first = orch.store.get_intent_for_subject(f"{attempt.id}:critic:1")
            second = orch.store.get_intent_for_subject(f"{attempt.id}:critic:2")
            assert first.state == "FAILED" and second.state == "SETTLED"
            assert first.agent_id != second.agent_id and provider.by_role["synthesizer"] == 2
            assert len(seen) == 2 and "feedback" not in seen[0]
            if mode == "schema":
                assert seen[1]["feedback"][0]["reason_code"] == "mission_criteria_mismatch"
            else:
                assert "feedback" not in seen[1]
            reservation = orch.commit.ledger.reservation(first.subject_id)
            if mode == "schema":
                assert reservation["settled_tokens"] == 150
            else:
                assert reservation["settled_tokens"] is None
                assert reservation["state"] == "RESERVED"
                assert orch.commit.ledger.has_unknown_usage(first.subject_id)

    asyncio.run(exercise())


async def _critic(commit, task, guard, runtime, *, priced=False):
    _, worker, _ = await _worker(commit, task, guard, runtime, priced=priced)
    subject = f"{worker.id}:critic:1"
    agent = await runtime.create(AgentConfig(
        name="critic", instructions="Review independently.", model_profile_ref="agent.general",
    ), creation_key=subject)
    intent = commit.create_service_intent(
        kind="critic", subject_id=subject, mission_id=task.mission_id,
        account_id=task_account(task.id), creation_key=subject,
        input_id="critic-input", input_hash="critic-fixture",
        config={
            "attempt_id": worker.id, "agent_config": agent.config.to_json(),
            "message": user_message_json("review"), "runtime_profile_id": "agent.general",
            "model": "agent-model", "provider_admission_fingerprint": guard.fingerprint,
            "provider_first_cost_micros": 3000 if priced else 0,
        }, reservation=Reservation(2000, 2000 if priced else 0),
        task_id=task.id, attempt_id=worker.id,
    )
    commit.claim_intent(intent.intent_id, owner="test-owner", lease_seconds=60)
    commit.record_agent_created(intent.intent_id, agent_id=agent.agent_id,
                                expected_turn_id=agent.turn_id_for(intent.input_id))
    return agent, intent


@pytest.mark.parametrize("priced", [False, True])
def test_critic_delta_and_known_unused_return_are_atomic_and_idempotent(tmp_path, priced):
    async def exercise():
        async with _runtime(tmp_path, priced=priced) as (
            commit, mission, task, hold, guard, provider, runtime,
        ):
            agent, intent = await _critic(commit, task, guard, runtime, priced=priced)
            accounts = _rows(commit, "budget_accounts")
            result = await agent.ask("review", input_id=intent.input_id, timeout=5)
            assert str(result.state) == "committed" and provider.calls == 1
            assert commit.ledger.reservation(intent.subject_id)["reserved_tokens"] == 5500
            assert commit.ledger.reservation(hold["subject_id"])["reserved_tokens"] == 2500
            assert _rows(commit, "budget_accounts") == accounts
            before = _rows(commit, "budget_accounts", "budget_reservations", "commit_receipts")
            with commit.store.transaction():
                guard.adapter.grow(commit.ledger.reservation(intent.subject_id), required=5500,
                                   required_cost_micros=5500 if priced else 0)
            assert _rows(commit, *before) == before
            facts = AgentBridge(runtime, unpriced=not priced).usage_facts(agent_id=agent.agent_id)
            with commit.store.transaction():
                commit.ledger.import_usage(subject_id=intent.subject_id, mission_id=mission.id,
                                           facts=facts)
            commit.settle_subject(intent.subject_id, mission.id, task_id=task.id)
            source = commit.ledger.reservation(hold["subject_id"])
            assert source["reserved_tokens"] == 7850  # 10K - 2K live Worker - 150 actual
            if priced:
                assert source["reserved_cost_micros"] == 7850
            after = _rows(commit, "budget_accounts", "budget_reservations", "commit_receipts")
            with commit.store.transaction():
                assert commit.ledger.import_usage(subject_id=intent.subject_id,
                                                  mission_id=mission.id, facts=facts) == 0
            commit.settle_subject(intent.subject_id, mission.id, task_id=task.id)
            assert _rows(commit, *after) == after

    asyncio.run(exercise())


@pytest.mark.parametrize("boundary", ["sibling_floor", "foreign_attempt", "foreign_account",
                                      "revision", "route", "cost"])
def test_critic_cannot_borrow_foreign_or_protected_funds(tmp_path, boundary):
    async def exercise():
        async with _runtime(tmp_path, priced=boundary == "cost") as (
            commit, _, task, hold, guard, provider, runtime,
        ):
            agent, intent = await _critic(commit, task, guard, runtime, priced=boundary == "cost")
            if boundary == "sibling_floor":
                await _worker(commit, task, guard, runtime, key="sibling")
            # Corrupt one durable binding at a time: simulate stale/foreign input,
            # not a replacement admission implementation or fabricated PASS.
            with commit.store.transaction():
                if boundary in {"foreign_attempt", "route"}:
                    current = commit.store.get_intent(intent.intent_id)
                    field, value = (
                        ("attempt_id", "foreign:attempt-1") if boundary == "foreign_attempt"
                        else ("model", "foreign-model")
                    )
                    commit.store.connection.execute(
                        "UPDATE dispatch_intents SET config_json=? WHERE intent_id=?",
                        (json.dumps({**dict(current.config), field: value}), intent.intent_id),
                    )
                if boundary == "foreign_account":
                    commit.store.connection.execute(
                        "UPDATE budget_reservations SET account_id=? WHERE subject_id=?",
                        ("budget:" + task.mission_id, intent.subject_id),
                    )
                if boundary == "revision":
                    commit.store.connection.execute(
                        "UPDATE budget_tail_holds SET task_revision='stale' WHERE hold_id=?",
                        (hold["hold_id"],),
                    )
                before = _rows(commit, "budget_accounts", "budget_reservations", "commit_receipts")
                with pytest.raises((BudgetError, ProviderAdmissionDenied)):
                    guard.adapter.grow(commit.ledger.reservation(intent.subject_id), required=5500,
                                       required_cost_micros=9000 if boundary == "cost" else 0)
                assert _rows(commit, *before) == before
            assert provider.calls == 0 and not grants(commit)

    asyncio.run(exercise())


def test_unknown_critic_growth_is_not_reissued_returned_or_borrowed(tmp_path):
    class Lost(ActualProvider):
        async def invoke(self, request, *, cancel):
            await super().invoke(request, cancel=cancel)
            raise RuntimeError("fixture response lost after actual handoff")

    class Evidence:
        async def observe(self, invocation):
            return ProviderReconciliationObservation(
                ProviderReconciliationState.STILL_UNKNOWN, "critic:" + invocation.invocation_id,
            )

    async def exercise():
        async with _runtime(tmp_path, provider=Lost(), evidence=Evidence()) as (
            commit, mission, task, _, guard, provider, runtime,
        ):
            agent, intent = await _critic(commit, task, guard, runtime)
            receipt = await agent.submit("review", input_id=intent.input_id)
            commit.record_submitted(intent.intent_id,
                                    receipt={"turn_id": receipt.turn_id, "seq": receipt.seq})
            await until(lambda: bool(grants(commit)) and grants(commit)[0]["state"] == "UNKNOWN")
            assert provider.calls == 1
            assert commit.ledger.reservation(intent.subject_id)["reserved_tokens"] == 5500
            before = _rows(commit, "budget_accounts", "budget_reservations", "commit_receipts")
            with commit.store.transaction():
                with pytest.raises(ProviderAdmissionDenied):
                    guard.adapter.grow(commit.ledger.reservation(intent.subject_id), required=6000)
                with pytest.raises(BudgetError, match="unknown"):
                    commit.settle_subject(intent.subject_id, mission.id, task_id=task.id)
            guard.recover(runtime.uow)
            assert _rows(commit, *before) == before and provider.calls == 1
            commit.cancel_mission(mission.id)
            assert commit.ledger.reservation(intent.subject_id)["state"] == "RESERVED"
            assert commit.ledger.reservation(intent.subject_id)["reserved_tokens"] == 5500

    asyncio.run(exercise())
