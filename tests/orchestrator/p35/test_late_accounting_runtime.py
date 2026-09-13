"""Late accounting is a money-only recovery, including already collected services.

Oracle: real SDK calls first omit usage; after cancellation and actual collection,
an original-identity accounting receipt must reach the original ancestor accounts
through Orchestrator.run/recover alone. No hand import/settle, second dispatch,
rewritten response, terminal Mission/Task/Claim, or duplicate budget event.
Unknown usage and foreign frozen bindings must keep the original money held.
"""

import asyncio
from dataclasses import replace

import pytest
from graph_helpers7 import node, spec
from test_provider_budget_guard import Counter
from test_succeeded_missing_usage_boundary import UsageOmitted

from agent_orchestrator.contracts import Budget
from agent_orchestrator.contracts.models import sha256_hex
from agent_orchestrator.graph.task_graph import TaskGraphProposal
from agent_orchestrator.orchestrator.commit_service import Reservation
from agent_orchestrator.orchestrator.event_handler import Orchestrator, OrchestratorConfig
from agent_orchestrator.runtime.agent_worker import user_message_json
from agent_orchestrator.runtime.assembly import PriceTable
from agent_orchestrator.runtime.model_router import RuntimeProfile
from simple_harness import RunId
from simple_harness.agents import AgentConfig
from simple_harness.providers import (
    ProviderAccountingIdentity,
    ProviderAccountingObservation,
    ProviderAccountingState,
    ProviderUsage,
)


def open_orch(root, provider):
    return Orchestrator(
        OrchestratorConfig(evidence_root=root),
        profiles={"default": RuntimeProfile(
            "default", provider, "agent-model",
            price_table=PriceTable("original", 1_000_000, 2_000_000),
            default_max_output_tokens=1000, max_output_tokens_ceiling=1000,
        )},
        provider_token_estimator=Counter(100),
    )


async def collected_unknown(orch, kind):
    mission = await orch.submit_mission(spec(
        conflict_reserve_tokens=0,
        budget=Budget(max_tokens=200000, max_cost_micros=20000, max_attempts=12),
    ))
    planning = orch.commit.begin_planning(mission.id)
    tasks, _ = orch.commit.commit_task_graph(
        mission.id, TaskGraphProposal.from_json({"tasks": [node("A", budget={
            "max_tokens": 20000, "max_cost_micros": 6000, "max_attempts": 3,
        })]}), base_version=planning.version, source={"planner": "legal graph fixture"},
    )
    message = user_message_json("Return the requested answer.")
    config = {
        "runtime_profile_id": "default", "model": "agent-model",
        "provider_admission_fingerprint": orch._provider_admission.fingerprint,
        "agent_config": AgentConfig(
            name=kind, instructions="Answer briefly.", model_profile_ref="default",
        ).to_json(),
        "message": message,
    }
    if kind == "manager":
        # Match the production Manager intent: its Task is explicit even when
        # this accounting-only fixture has no historical Attempt reference.
        config["task_id"] = tasks[0].id
    if kind == "attempt":
        _, intent = orch.commit.create_attempt(
            tasks[0].id, role="worker", model="agent-model", prompt_version="worker-v2",
            context_version="ctx", reservation=Reservation(4000, 4000),
            intent_config=config, input_hash=sha256_hex(message),
        )
    else:
        intent = orch.commit.create_service_intent(
            kind=kind, subject_id=f"{mission.id}:{kind}:accounting", mission_id=mission.id,
            account_id="budget:" + mission.id, creation_key=f"accounting:{kind}",
            input_id=f"accounting:{kind}", input_hash=sha256_hex(message),
            config=config, reservation=Reservation(4000, 4000),
        )
    bridge = orch.bridge_for(intent)
    orch.commit.claim_intent(intent.intent_id, owner=orch._owner, lease_seconds=60)
    agent_id, _, _ = await bridge.create(
        creation_key=intent.creation_key, config_json=config["agent_config"],
    )
    turn_id = await bridge.expected_turn_id(agent_id=agent_id, input_id=intent.input_id)
    orch.commit.record_agent_created(intent.intent_id, agent_id=agent_id, expected_turn_id=turn_id)
    receipt = await bridge.submit(agent_id=agent_id, input_id=intent.input_id, message_json=message)
    intent = orch.commit.record_submitted(intent.intent_id, receipt=receipt)
    for _ in range(1000):
        if await bridge.result(agent_id=agent_id, turn_id=turn_id) is not None:
            break
        await asyncio.sleep(.002)
    else:
        raise AssertionError("actual SDK turn did not finish")
    original, = bridge.runtime.uow.list_provider_invocations(RunId(agent_id))
    assert original.handoff_attempt == 1
    assert original.usage_json.get("usage") is None
    orch.commit.cancel_mission(mission.id)
    assert await orch._collect_after_stop(intent)
    assert orch.store.get_intent(intent.intent_id).state in {"SETTLED", "FAILED"}
    assert orch.commit.ledger.reservation(intent.subject_id)["state"] == "RESERVED"
    return orch.store.get_intent(intent.intent_id), original


def business(orch, mission_id):
    return (
        orch.store.get_mission(mission_id).to_json(),
        [t.to_json() for t in orch.store.list_tasks(mission_id)],
        [c.to_json() for c in orch.store.list_mission_claims(mission_id)],
    )


def record_receipt(orch, intent, original, usage):
    # The public SDK writer validates original identity/price; it does not import
    # into the Orchestrator ledger. This is the cross-database crash boundary.
    orch.bridge_for(intent).runtime.uow.record_provider_accounting(
        original, observation=ProviderAccountingObservation(
            ProviderAccountingState.KNOWN, ProviderAccountingIdentity.from_record(original),
            "original-provider-accounting:" + original.invocation_id, usage,
        ), now=orch.store.now,
    )


@pytest.mark.parametrize("kind", ["attempt", "plan", "critic", "manager"])
@pytest.mark.parametrize("cold", [False, True])
def test_terminal_collected_original_receipt_is_automatically_imported_once(tmp_path, kind, cold):
    async def exercise():
        provider = UsageOmitted()
        async with open_orch(tmp_path, provider) as orch:
            intent, original = await collected_unknown(orch, kind)
            frozen = business(orch, intent.mission_id)
            await orch.run(max_cycles=3)
            assert orch.commit.ledger.reservation(intent.subject_id)["state"] == "RESERVED"
            record_receipt(orch, intent, original, provider.actual.usage)
            if not cold:
                await orch._cycle()  # receipt arrived after this process's recover
                assert_paid(orch, intent, original, frozen, provider)
                return
        async with open_orch(tmp_path, provider) as reopened:
            await reopened.run(max_cycles=3)
            assert_paid(reopened, intent, original, frozen, provider)
            count = len(reopened.store.list_events(intent.mission_id))
            await reopened.run(max_cycles=3)
            assert len(reopened.store.list_events(intent.mission_id)) == count
            assert_paid(reopened, intent, original, frozen, provider)
    asyncio.run(exercise())


def assert_paid(orch, intent, original, frozen, provider):
    reservation = orch.commit.ledger.reservation(intent.subject_id)
    assert reservation["state"] == "SETTLED"
    assert reservation["settled_tokens"] == 150 and reservation["settled_cost_micros"] == 200
    for account in orch.commit.ledger._chain(reservation["account_id"]):
        assert account.settled_tokens == 150 and account.settled_cost_micros == 200
        assert account.reserved_tokens == account.reserved_cost_micros == 0
    if intent.kind == "manager":
        task_account = orch.commit.ledger.account("budget:" + intent.config["task_id"])
        assert task_account.settled_tokens == task_account.reserved_tokens == 0
        assert task_account.settled_cost_micros == task_account.reserved_cost_micros == 0
    assert business(orch, intent.mission_id) == frozen
    uow = orch.bridge_for(intent).runtime.uow
    assert uow.read_provider_invocation(original.invocation_id) == original
    assert provider.calls == 1


@pytest.mark.parametrize("field", ["expected_turn_id", "input_hash"])
def test_foreign_frozen_binding_does_not_import_or_release(tmp_path, field):
    async def exercise():
        provider = UsageOmitted()
        async with open_orch(tmp_path, provider) as orch:
            intent, original = await collected_unknown(orch, "plan")
            record_receipt(orch, intent, original, provider.actual.usage)
            with orch.store.transaction():
                orch.store.connection.execute(
                    f"UPDATE dispatch_intents SET {field}=? WHERE intent_id=?",
                    ("foreign", intent.intent_id),
                )
            assert getattr(orch.store.get_intent(intent.intent_id), field) == "foreign"
            await orch._cycle()
            assert orch.commit.ledger.reservation(intent.subject_id)["state"] == "RESERVED"
            assert orch.commit.ledger.usage_for(intent.subject_id)[0] == 0
            assert provider.calls == 1
            if field == "input_hash":
                assert any("late accounting binding: frozen input hash" in line
                           for line in orch.progress_log)
    asyncio.run(exercise())


class OverrunOmitted(UsageOmitted):
    async def invoke(self, request, *, cancel):
        response = await super().invoke(request, cancel=cancel)
        # This fixture is the external provider's authoritative actual usage,
        # deliberately exceeding the admitted 100 input tokens. No SDK SQL edit.
        self.actual = replace(self.actual, usage=ProviderUsage(5000, 50, 5050))
        return response


def test_late_actual_overrun_is_paid_before_recovery_refusal_without_business_rewrite(tmp_path):
    async def exercise():
        provider = OverrunOmitted()
        async with open_orch(tmp_path, provider) as orch:
            intent, original = await collected_unknown(orch, "plan")
            frozen = business(orch, intent.mission_id)
            record_receipt(orch, intent, original, provider.actual.usage)
        async with open_orch(tmp_path, provider) as reopened:
            await reopened.run(max_cycles=3)
            reservation = reopened.commit.ledger.reservation(intent.subject_id)
            assert reservation["state"] == "SETTLED"
            assert reservation["settled_tokens"] == 5050
            assert reservation["settled_cost_micros"] == 5100
            grant = reopened.store.connection.execute(
                "SELECT * FROM provider_token_grants WHERE subject_id=?", (intent.subject_id,),
            ).fetchone()
            assert grant["state"] == "OVERRUN" and grant["actual_cost_micros"] == 5100
            assert business(reopened, intent.mission_id) == frozen
            assert provider.calls == 1
            before = len(reopened.store.list_events(intent.mission_id))
            await reopened.run(max_cycles=3)
            assert len(reopened.store.list_events(intent.mission_id)) == before
    asyncio.run(exercise())
