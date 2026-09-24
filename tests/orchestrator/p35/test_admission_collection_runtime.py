# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""Actual SDK admission -> durable terminal -> Orchestrator collect, no fake errors.

Oracle: a denied physical handoff never spends another Attempt or escalates a
model. A genuine unknown earlier handoff keeps its reservation while waiting.
The original Task budget is immutable; Mission spare budget is not a top-up.
"""

import asyncio
from dataclasses import replace

import pytest

from agent_orchestrator.contracts import Budget, MissionStatus, TaskStatus
from agent_orchestrator.graph.task_graph import TaskGraphProposal
from agent_orchestrator.orchestrator.commit_service import CommitRejected, MissionSpec, Reservation
from agent_orchestrator.orchestrator.event_handler import Orchestrator, OrchestratorConfig
from agent_orchestrator.runtime.model_router import RuntimeProfile
from agent_orchestrator.testing.fixtures import MODEL, RoleScriptedProvider


class Counter:
    fingerprint = "admission-collection-fixture-v1"
    bound_protocol = "fixture-input-allowance-v1"
    requires_prior_output_reserve = True

    def __init__(self, reason):
        self.reason = reason

    def estimate_input_tokens(self, request):
        if self.reason == "estimator_unavailable":
            raise ValueError("counter unavailable")
        return 90_001 if self.reason == "budget_exhausted" else 100


class MissingUsageProvider(RoleScriptedProvider):
    async def invoke(self, request, *, cancel):
        response = await super().invoke(request, cancel=cancel)
        return replace(response, usage=None)


def _grants(orch, subject_id):
    return [
        dict(row)
        for row in orch.store.connection.execute(
            "SELECT state, actual_tokens, actual_output_tokens, total_upper FROM provider_token_grants"
            " WHERE subject_id=? ORDER BY created_at, invocation_id",
            (subject_id,),
        )
    ]


def test_a_missing_usage_report_is_bounded_and_never_pauses_the_task(tmp_path):
    """Rule 2026-09-24 (may overcount, never undercount): a successful call whose gateway
    reported no usage used to hold the Agent's next call (``usage_unresolved``).  Its grant
    stays UNKNOWN with the reservation held (late accounting may resolve it), but the next
    call is admitted with that call's output bounded by its own cap."""

    async def exercise():
        provider = MissingUsageProvider({"worker": [("workspace_list", {})] * 3})
        profile = RuntimeProfile("default", provider, MODEL, default_max_output_tokens=1000, max_output_tokens_ceiling=1000)
        config = OrchestratorConfig(evidence_root=tmp_path, max_concurrency=1, candidates_per_task=1, attempt_reserve_tokens=4000)
        async with Orchestrator(config, profiles={"default": profile}, provider_token_estimator=Counter("ok")) as orch:
            mission = await orch.submit_mission(
                MissionSpec(
                    "Write the full original deliverable", ("file:answer.txt",), "test", "admission",
                    allowed_tools=("workspace_list", "workspace_write_file"), budget=Budget(max_tokens=400000, max_attempts=10),
                )
            )
            planning = orch.commit.begin_planning(mission.id)
            tasks, _ = orch.commit.commit_task_graph(
                mission.id,
                TaskGraphProposal.from_json({"tasks": [{
                    "key": "A", "goal": "Write the full original deliverable", "rationale": "missing usage", "dependencies": [],
                    "success_criteria": ["file:answer.txt"], "verification_policy": ["format_check", "rule_check"],
                    "allowed_tools": ["workspace_list", "workspace_write_file"], "budget": {"max_tokens": 90000, "max_attempts": 3},
                }]}),
                base_version=planning.version, source={"planner": "fixture"},
            )
            task = tasks[0]
            for _ in range(800):
                await orch._cycle()
                current = orch.store.get_task(task.id)
                if provider.calls >= 2 or current.paused or current.status is TaskStatus.FAILED:
                    break
                await asyncio.sleep(0.002)
            current = orch.store.get_task(task.id)
            assert not current.paused, current.pause_reason
            assert provider.calls >= 2, "the second call must be admitted after a usage-less first call"
            attempt = orch.store.list_attempts(task.id)[0]
            first = _grants(orch, attempt.id)[0]
            assert first["state"] == "UNKNOWN" and first["actual_tokens"] is None and first["total_upper"] == 1100
            with orch.store.transaction():
                assert orch.commit.ledger.has_unknown_usage(attempt.id)

    asyncio.run(exercise())


@pytest.mark.parametrize("reason", ["budget_exhausted", "estimator_unavailable"])
def test_actual_guard_denial_stops_or_waits_without_another_attempt(tmp_path, reason):
    async def exercise():
        provider_type = RoleScriptedProvider
        provider = provider_type({"worker": [("workspace_list", {})] * 3})
        profile = RuntimeProfile(
            "default",
            provider,
            MODEL,
            default_max_output_tokens=1000,
            max_output_tokens_ceiling=1000,
        )
        config = OrchestratorConfig(
            evidence_root=tmp_path,
            max_concurrency=1,
            candidates_per_task=1,
            attempt_reserve_tokens=4000,
        )
        async with Orchestrator(
            config, profiles={"default": profile}, provider_token_estimator=Counter(reason)
        ) as orch:
            mission = await orch.submit_mission(
                MissionSpec(
                    "Write the full original deliverable",
                    ("file:answer.txt",),
                    "test",
                    "admission",
                    allowed_tools=("workspace_list", "workspace_write_file"),
                    budget=Budget(max_tokens=400000, max_attempts=10),
                )
            )
            planning = orch.commit.begin_planning(mission.id)
            tasks, _ = orch.commit.commit_task_graph(
                mission.id,
                TaskGraphProposal.from_json(
                    {
                        "tasks": [
                            {
                                "key": "A",
                                "goal": "Write the full original deliverable",
                                "rationale": "original 90k contract",
                                "dependencies": [],
                                "success_criteria": ["file:answer.txt"],
                                "verification_policy": ["format_check", "rule_check"],
                                "allowed_tools": ["workspace_list", "workspace_write_file"],
                                "budget": {"max_tokens": 90000, "max_attempts": 3},
                            }
                        ],
                    }
                ),
                base_version=planning.version,
                source={"planner": "fixture"},
            )
            task = tasks[0]
            for _ in range(500):
                await orch._cycle()
                current = orch.store.get_task(task.id)
                if current.status is TaskStatus.FAILED or current.paused:
                    break
                await asyncio.sleep(0.002)
            else:
                raise AssertionError("actual admission denial did not reach stop/wait")

            attempts = orch.store.list_attempts(task.id)
            assert len(attempts) == 1
            attempt = attempts[0]
            error = attempt.failure["error"]
            assert error["error_code"] == "provider_admission_denied"
            assert error["source_kind"] == "provider_admission" and error["retryable"] is False
            assert error["detail"]["schema_version"] == 1
            assert error["detail"]["reason_code"] == reason
            assert attempt.failure["reason"] == "provider_admission_denied"
            assert current.budget == task.budget
            assert orch.store.get_mission(mission.id).budget.max_tokens == 400000
            assert provider.calls == (1 if reason == "usage_unresolved" else 0)
            intent = orch.store.get_intent_for_subject(attempt.id)
            assert intent is not None
            assert intent.state == "FAILED"
            if reason == "usage_unresolved":
                assert (
                    current.paused and current.pause_reason == "provider_admission:usage_unresolved"
                )
                assert orch.store.get_mission(mission.id).status is MissionStatus.ACTIVE
                with orch.store.transaction():
                    assert orch.commit.ledger.has_unknown_usage(attempt.id)
                    assert orch.commit.ledger.reservation(attempt.id)["reserved_tokens"] > 0
                with pytest.raises(CommitRejected, match="admission"):
                    orch.commit.create_attempt(
                        task.id,
                        role="worker",
                        model=MODEL,
                        prompt_version="worker-v2",
                        context_version="ctx",
                        reservation=Reservation(4000, 0),
                        intent_config={},
                        input_hash="new",
                    )
            else:
                assert current.status is TaskStatus.FAILED
                stopped = orch.store.get_mission(mission.id)
                assert stopped.status is MissionStatus.FAILED
                assert stopped.stop_reason == (
                    "budget_exhausted" if reason == "budget_exhausted" else "runtime_unavailable"
                )
                assert stopped.final_report["detail"]["admission"]["reason_code"] == reason
            before = len(orch.store.list_events(mission.id))
            for _ in range(5):
                await orch._cycle()
            assert len(orch.store.list_attempts(task.id)) == 1
            assert provider.calls == (1 if reason == "usage_unresolved" else 0)
            assert len(orch.store.list_events(mission.id)) == before

    asyncio.run(exercise())


class EmptyFirstProvider(RoleScriptedProvider):
    """The day-card gateway's failure mode: the first call is an empty completion with no
    usage (a definite failure); later calls are normal."""

    empty_served = False

    async def invoke(self, request, *, cancel):
        if not self.empty_served:
            self.empty_served = True
            response = await super().invoke(request, cancel=cancel)
            from simple_harness import Message, MessageRole

            return replace(response, message=Message(MessageRole.ASSISTANT, ""), tool_calls=(), usage=None, finish_reason="stop")
        return await super().invoke(request, cancel=cancel)


def test_one_empty_reply_does_not_pause_the_task(tmp_path):
    """RP-E4 (real model): an empty reply without usage must not stall the Mission.  Each
    retry is a new Attempt (a new Agent run), so no prior-usage hold applies; the failed
    Attempt's own usage stays UNKNOWN and keeps its reservation held — conservative
    accounting, never a freeze."""

    async def exercise():
        provider = EmptyFirstProvider({"worker": [("workspace_list", {})] * 6})
        profile = RuntimeProfile("default", provider, MODEL, default_max_output_tokens=1000, max_output_tokens_ceiling=1000)
        config = OrchestratorConfig(evidence_root=tmp_path, max_concurrency=1, candidates_per_task=1, attempt_reserve_tokens=4000)
        async with Orchestrator(config, profiles={"default": profile}, provider_token_estimator=Counter("ok")) as orch:
            mission = await orch.submit_mission(
                MissionSpec(
                    "Write the full original deliverable", ("file:answer.txt",), "test", "admission",
                    allowed_tools=("workspace_list", "workspace_write_file"), budget=Budget(max_tokens=400000, max_attempts=10),
                )
            )
            planning = orch.commit.begin_planning(mission.id)
            tasks, _ = orch.commit.commit_task_graph(
                mission.id,
                TaskGraphProposal.from_json({"tasks": [{
                    "key": "A", "goal": "Write the full original deliverable", "rationale": "empty reply", "dependencies": [],
                    "success_criteria": ["file:answer.txt"], "verification_policy": ["format_check", "rule_check"],
                    "allowed_tools": ["workspace_list", "workspace_write_file"], "budget": {"max_tokens": 90000, "max_attempts": 3},
                }]}),
                base_version=planning.version, source={"planner": "fixture"},
            )
            task = tasks[0]
            for _ in range(800):
                await orch._cycle()
                current = orch.store.get_task(task.id)
                if provider.calls >= 2 or current.paused or current.status is TaskStatus.FAILED:
                    break
                await asyncio.sleep(0.002)
            current = orch.store.get_task(task.id)
            assert not (current.paused and current.pause_reason == "provider_admission:usage_unresolved"), current.pause_reason
            assert provider.calls >= 2, "the Task must go on after one empty reply"
            first = orch.store.list_attempts(task.id)[0]
            with orch.store.transaction():
                assert orch.commit.ledger.has_unknown_usage(first.id)
                assert orch.commit.ledger.reservation(first.id)["reserved_tokens"] > 0

    asyncio.run(exercise())
