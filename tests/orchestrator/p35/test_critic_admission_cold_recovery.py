# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""Reopen a closed SDK/Orchestrator library between denial and Task stop.

Only the crash location is injected. Provider calls, admission denial, durable
Critic failure, verification, accounting and recovery use the actual runtime.
"""

import asyncio

import pytest
from test_system_critic_hold_growth import _scenario

from agent_orchestrator.contracts import MissionStatus, TaskStatus
from agent_orchestrator.orchestrator.commit_service import task_account
from agent_orchestrator.orchestrator.event_handler import Orchestrator
from agent_orchestrator.runtime.model_router import RuntimeProfile
from agent_orchestrator.storage.store import InjectedCrash
from simple_harness.agents.context.budget import ContextPolicy
from simple_harness.contracts import RunId


def _sdk_records(orch):
    """Original request identities and handoffs, including the denied request."""
    records = []
    for intent in orch.store.list_intents():
        if intent.agent_id is None:
            continue
        uow = orch.bridge_for(intent).runtime.uow
        binding = uow.read_agent_binding(intent.agent_id)
        assert binding is not None
        for row in uow.list_provider_invocations(RunId(binding.run_id)):
            records.append((
                row.invocation_id, row.request_fingerprint, str(row.state),
                row.handoff_attempt, row.rehandoff_count,
                None if row.usage_json is None else dict(row.usage_json),
            ))
    return sorted(records, key=lambda row: row[0])


def _known_usage(orch):
    return [tuple(row) for row in orch.store.connection.execute(
        "SELECT usage_ref,subject_id,input_tokens,output_tokens,cost_micros,unpriced,unknown "
        "FROM imported_usage ORDER BY usage_ref",
    )]


@pytest.mark.parametrize("gap", ["critic_failed", "critic_error_recorded"])
def test_cold_critic_admission_failure_stops_original_task_without_new_handoff(
    tmp_path, monkeypatch, gap,
):
    async def exercise():
        config, mission_spec, provider, estimator, _ = _scenario(tmp_path, "budget")

        def open_library():
            return Orchestrator(
                config, provider, provider_token_estimator=estimator,
                profiles={"default": RuntimeProfile(
                    "default", provider, config.model,
                    context_policy=ContextPolicy(max_input_tokens=32768, render_slack_tokens=0),
                )},
            )

        crashes = []
        async with open_library() as first:
            mission = await first.submit_mission(mission_spec)
            old_store = first.store
            settle = first._settle_intent
            record = first.commit.record_verification_layer

            def after_failed(intent, state):
                settle(intent, state)
                if gap == "critic_failed" and intent.kind == "critic" and state == "FAILED":
                    assert first.store.get_intent(intent.intent_id).state == "FAILED"
                    crashes.append(gap)
                    raise InjectedCrash(gap)

            def after_error(result_id, *, layer, status, detail):
                record(result_id, layer=layer, status=status, detail=detail)
                if (gap == "critic_error_recorded"
                        and layer == "critic_review" and status == "ERROR"):
                    assert detail["error"]["source_kind"] == "provider_admission"
                    assert detail["error"]["retryable"] is False
                    crashes.append(gap)
                    raise InjectedCrash(gap)

            with monkeypatch.context() as patch:
                patch.setattr(first, "_settle_intent", after_failed)
                patch.setattr(first.commit, "record_verification_layer", after_error)
                with pytest.raises(InjectedCrash, match=gap):
                    await asyncio.wait_for(first.run(), 30)

            assert crashes == [gap]
            task = next(t for t in first.store.list_tasks(mission.id) if t.kind == "synthesis")
            [attempt] = first.store.list_attempts(task.id)
            subject = f"{attempt.id}:critic:1"
            intent = first.store.get_intent_for_subject(subject)
            assert intent is not None and intent.state == "FAILED"
            assert first.store.get_mission(mission.id).status is MissionStatus.ACTIVE
            assert task.status is TaskStatus.VERIFYING
            stored = first.store.find_result_for_attempt(attempt.id)
            assert stored is not None and stored.verification_state == "RUNNING"
            assert not any(e.type == "VerificationFailed" and e.attempt_id == attempt.id
                           for e in first.store.list_events(mission.id))
            layers = [r for r in first.store.list_verifications(stored.envelope.id)
                      if r["layer"] == "critic_review"]
            assert len(layers) == (1 if gap == "critic_error_recorded" else 0)
            if layers:
                assert layers[0]["status"] == "ERROR"

            sdk_result = await first.bridge_for(intent).result(
                agent_id=intent.agent_id, turn_id=intent.expected_turn_id,
            )
            assert sdk_result is not None and str(sdk_result.state) == "failed"
            error = sdk_result.error
            assert error["source_kind"] == "provider_admission" and error["retryable"] is False
            assert error["detail"]["reason_code"] == "budget_exhausted"
            assert error["detail"]["request_tokens"] == 41_192
            expected_error = error

            # Eight known 25K replies, then the ninth request is denied BEFORE handoff.
            assert provider.by_role["critic"] == 8 and provider.by_role["synthesizer"] == 2
            uow = first.bridge_for(intent).runtime.uow
            binding = uow.read_agent_binding(intent.agent_id)
            invocations = uow.list_provider_invocations(RunId(binding.run_id))
            assert len(invocations) == 9
            assert sum(r.handoff_attempt for r in invocations) == 8
            assert sum(r.handoff_attempt == 0 for r in invocations) == 1
            usage_before = _known_usage(first)
            critic_usage = [row for row in usage_before if row[1] == subject]
            assert len(critic_usage) == 8
            assert sum(row[2] + row[3] for row in critic_usage) == 200_000
            assert all(row[6] == 0 for row in critic_usage)
            reservation = first.commit.ledger.reservation(subject)
            assert reservation["state"] == (
                "RESERVED" if gap == "critic_failed" else "SETTLED"
            )
            if gap == "critic_error_recorded":
                assert reservation["settled_tokens"] == 200_000
            calls_before = provider.calls
            sdk_before = _sdk_records(first)
            intent_before = intent.to_json()

        # New Orchestrator, new Store and reopened SDK library; no in-memory
        # verification task, bound tool workspace or local error variable survives.
        assert first._store is None and first._assembled is None
        async with open_library() as reopened:
            assert reopened is not first and reopened.store is not old_store
            assert reopened.store.get_intent(intent.intent_id).to_json() == intent_before
            await asyncio.wait_for(reopened.run(), 30)
            finished = reopened.store.get_mission(mission.id)
            assert finished.status is MissionStatus.FAILED
            assert finished.stop_reason == "budget_exhausted"
            stopped = reopened.store.get_task(task.id)
            assert stopped.status is TaskStatus.FAILED and stopped.accepted_result_id is None
            assert [a.id for a in reopened.store.list_attempts(task.id)] == [attempt.id]
            assert reopened.store.get_intent_for_subject(f"{attempt.id}:critic:2") is None
            assert reopened.store.get_intent(intent.intent_id).to_json() == intent_before
            assert provider.calls == calls_before
            assert _sdk_records(reopened) == sdk_before
            assert _known_usage(reopened) == usage_before
            reservation = reopened.commit.ledger.reservation(subject)
            assert reservation["state"] == "SETTLED" and reservation["settled_tokens"] == 200_000
            account = reopened.commit.ledger.account(task_account(task.id))
            assert account.settled_tokens == 200_300 and account.reserved_tokens == 0
            failures = [e for e in reopened.store.list_events(mission.id)
                        if e.type == "VerificationFailed" and e.attempt_id == attempt.id]
            assert len(failures) == 1
            assert failures[0].payload["failures"][0]["detail"]["error"] == expected_error
            assert not any(e.type == "VerificationPassed" and e.attempt_id == attempt.id
                           for e in reopened.store.list_events(mission.id))

            # A further recovery cycle cannot charge or hand off the denial again.
            await asyncio.wait_for(reopened.run(), 5)
            assert provider.calls == calls_before and _sdk_records(reopened) == sdk_before
            assert _known_usage(reopened) == usage_before
            assert reopened.commit.ledger.account(task_account(task.id)).settled_tokens == 200_300

    asyncio.run(exercise())
