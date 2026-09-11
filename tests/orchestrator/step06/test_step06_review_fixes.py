# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501

"""Step 6 · code review round 1 dispositions (reports/code-review-round1.md): one
decisive test per P0/P1 fix and for the P2 fixes that changed behaviour."""

from __future__ import annotations

import asyncio
import json

import pytest
from helpers_step06 import config, events_of, only, spec

from agent_orchestrator.contracts import AttemptStatus, Budget, MissionStatus
from agent_orchestrator.governance.budgets import UsageFact
from agent_orchestrator.observability.evidence import write_evidence
from agent_orchestrator.orchestrator.commit_service import mission_account
from agent_orchestrator.orchestrator.event_handler import Orchestrator
from agent_orchestrator.runtime.model_router import (
    ModelRouter,
    RoutingRules,
    RoutingUnavailable,
    RuntimeProfile,
    classify_turn_error,
)
from agent_orchestrator.storage.store import InjectedCrash
from agent_orchestrator.testing.fixtures import (
    UnavailableProvider,
    _recorder_task,
    _write_files_then,
    demo_dynamic_dag_provider,
    graph_proposal_step,
)

FAKE_KEY = "sk-" + "a1b2c3d4e5f6g7h8i9j0k1l2"  # shaped like a key, not a key
SERVICES_LARGE = {"planner": "large", "critic": "large", "manager": "large"}


def _doc(key, *, policy=("format_check", "rule_check")):
    return _recorder_task(
        key,
        f"写出文档 {key}.md（独立任务 {key}）",
        [],
        [f"file:{key}.md"],
        1.0,
        [f"{key}.md"],
        policy=list(policy),
    )


def _doc_script(key, content=None):
    return _write_files_then(
        {f"{key}.md": content or f"# {key}\n"},
        test_path=None,
        summary=f"{key} 写出",
        claim=f"{key}.md 写出",
    )


# ------------------------------------------------------------------ P0-1 service pools down
def test_p0_1_a_down_planner_pool_waits_bounded_and_never_crashes_the_loop(tmp_path):
    down = UnavailableProvider(model="fixture-down")
    small = demo_dynamic_dag_provider(tasks=only("A"), model="fixture-small")
    profiles = {
        "small": RuntimeProfile("small", small, "fixture-small"),
        "large": RuntimeProfile("large", down, "fixture-down", tier=2),
    }
    rules = RoutingRules(default="small", by_role=SERVICES_LARGE)

    async def case():
        async with Orchestrator(
            config(
                tmp_path,
                profile_failure_threshold=1,
                profile_cooldown_seconds=30.0,
                profile_wait_seconds=0.4,
            ),
            profiles=profiles,
            routing=rules,
            poll_interval=0.02,
        ) as orchestrator:
            mission = await orchestrator.submit_mission(
                spec("p0-1a", success_criteria=("file:analysis.md",))
            )
            await asyncio.wait_for(
                orchestrator.run(), 30
            )  # RoutingUnavailable never escapes the loop
            final = orchestrator.store.get_mission(mission.id)
            assert (
                final.status is MissionStatus.FAILED and final.stop_reason == "runtime_unavailable"
            ), orchestrator.progress_log
            assert final.final_report["detail"]["profile_id"] == "large"
            assert final.final_report["detail"]["waited_seconds"] >= 0.4
            assert down.calls == 1  # probed once, then the cooldown held
            assert not orchestrator._deferred_planning
            assert events_of(orchestrator.store, mission.id, "RuntimeProfileUnavailable")

    asyncio.run(case())


def test_p0_1_a_down_critic_pool_makes_the_layer_an_error_never_a_pass_and_never_crashes(tmp_path):
    task = _doc("A", policy=("format_check", "rule_check", "critic_review"))
    down = UnavailableProvider(model="fixture-down")
    small = demo_dynamic_dag_provider(
        tasks=[task], per_attempt={"A": [_doc_script("A")] * 3}, model="fixture-small"
    )
    profiles = {
        "small": RuntimeProfile("small", small, "fixture-small"),
        "large": RuntimeProfile("large", down, "fixture-down", tier=2),
    }
    rules = RoutingRules(default="small", by_role={"critic": "large"})

    async def case():
        async with Orchestrator(
            config(
                tmp_path,
                profile_failure_threshold=1,
                profile_cooldown_seconds=30.0,
                manager_after_failures=10,
            ),
            profiles=profiles,
            routing=rules,
        ) as orchestrator:
            mission = await orchestrator.submit_mission(
                spec("p0-1b", success_criteria=("file:A.md",))
            )
            await asyncio.wait_for(orchestrator.run(), 60)
            store = orchestrator.store
            final = store.get_mission(mission.id)
            assert (
                final.status is MissionStatus.FAILED and final.stop_reason == "max_attempts_reached"
            ), orchestrator.progress_log
            attempts = store.list_attempts(store.list_tasks(mission.id)[0].id)
            assert len(attempts) == 3
            for attempt in attempts:
                layers = {
                    v["layer"]: v
                    for v in store.list_verifications(
                        store.find_result_for_attempt(attempt.id).envelope.id
                    )
                }
                assert layers["critic_review"]["status"] == "ERROR"
            assert store.count_events(mission.id, "VerificationPassed") == 0
            assert down.calls == 1  # the Critic's pool tripped on its first failure (review P2-2)

    asyncio.run(case())


# ------------------------------------------------------------------ P0-2 deferred leak
def test_p0_2_a_task_waiting_for_its_pool_does_not_keep_run_alive_after_its_mission_ends(tmp_path):
    down = UnavailableProvider(model="fixture-down")
    large = demo_dynamic_dag_provider(tasks=only("A"), model="fixture-large")
    profiles = {
        "small": RuntimeProfile("small", down, "fixture-down"),
        "large": RuntimeProfile("large", large, "fixture-large", tier=2),
    }
    rules = RoutingRules(default="small", by_role=SERVICES_LARGE)

    async def case():
        async with Orchestrator(
            config(
                tmp_path,
                profile_failure_threshold=1,
                profile_cooldown_seconds=60.0,
                profile_wait_seconds=60.0,
                manager_after_failures=10,
            ),
            profiles=profiles,
            routing=rules,
            poll_interval=0.02,
        ) as orchestrator:
            mission = await orchestrator.submit_mission(
                spec(
                    "p0-2",
                    success_criteria=("file:analysis.md",),
                    budget=Budget(max_tokens=300_000, max_attempts=8, max_runtime_seconds=1),
                )
            )
            await asyncio.wait_for(
                orchestrator.run(), 20
            )  # returns: the wait ended with its Mission
            final = orchestrator.store.get_mission(mission.id)
            assert (
                final.status is MissionStatus.FAILED and final.stop_reason == "budget_exhausted"
            ), orchestrator.progress_log
            assert final.final_report["detail"]["dimension"] == "runtime"
            assert orchestrator._deferred == {}

    asyncio.run(case())


# ------------------------------------------------------------------ P1-1 Global scope
def test_p1_1_global_pool_exhaustion_stops_the_mission_and_blames_no_task(tmp_path):
    task = _doc("A")
    task["budget"] = {"max_tokens": 30_000, "max_attempts": 1}  # fits a one-attempt Mission
    provider = demo_dynamic_dag_provider(
        tasks=[task],
        planner_steps=[graph_proposal_step([task])] * 2,
        per_attempt={"A": [_doc_script("A")] * 2},
    )

    async def case():
        async with Orchestrator(
            config(tmp_path, global_budget=Budget(max_attempts=1)), provider
        ) as orchestrator:
            one = await orchestrator.submit_mission(
                spec(
                    "g-1",
                    success_criteria=("file:A.md",),
                    budget=Budget(max_tokens=300_000, max_attempts=1),
                )
            )
            two = await orchestrator.submit_mission(
                spec(
                    "g-2",
                    success_criteria=("file:A.md",),
                    budget=Budget(max_tokens=300_000, max_attempts=1),
                )
            )
            await asyncio.wait_for(orchestrator.run(), 60)
            store = orchestrator.store
            finals = [store.get_mission(one.id), store.get_mission(two.id)]
            assert sorted(str(m.status) for m in finals) == ["COMPLETED", "FAILED"], (
                orchestrator.progress_log
            )
            failed = next(m for m in finals if m.status is MissionStatus.FAILED)
            assert (
                failed.stop_reason == "budget_exhausted"
            )  # not max_attempts_reached: no Task is to blame
            detail = failed.final_report["detail"]
            assert (
                detail["scope"] == "global"
                and detail["dimension"] == "attempts"
                and detail["account"] == "budget:global"
            )
            assert "failed_task_id" not in failed.final_report

    asyncio.run(case())


def test_p1_1_global_pool_exhaustion_in_planning_names_the_global_scope(tmp_path):
    provider = demo_dynamic_dag_provider(
        tasks=only("A"), planner_steps=[graph_proposal_step(only("A"))] * 2
    )

    async def case():
        async with Orchestrator(
            config(tmp_path, global_budget=Budget(max_tokens=6_000)), provider
        ) as orchestrator:
            await orchestrator.submit_mission(
                spec("gp-1", success_criteria=("file:analysis.md",), budget=Budget(max_attempts=4))
            )
            second = await orchestrator.submit_mission(
                spec("gp-2", success_criteria=("file:analysis.md",), budget=Budget(max_attempts=4))
            )
            await asyncio.wait_for(orchestrator.run(), 60)
            final = orchestrator.store.get_mission(second.id)
            assert final.status is MissionStatus.FAILED and final.stop_reason == "budget_exhausted"
            assert (
                final.final_report["detail"]["scope"] == "global"
                and final.final_report["detail"]["phase"] == "planning"
            )

    asyncio.run(case())


# ------------------------------------------------------------------ P1-2 tool calls in parallel
def test_p1_2_parallel_tasks_share_the_tool_call_pool_without_a_false_exhaustion(tmp_path):
    tasks = [_doc("P1"), _doc("P2")]
    provider = demo_dynamic_dag_provider(
        tasks=tasks, per_attempt={"P1": [_doc_script("P1")], "P2": [_doc_script("P2")]}
    )

    async def case():
        async with Orchestrator(config(tmp_path, max_concurrency=2), provider) as orchestrator:
            mission = await orchestrator.submit_mission(
                spec(
                    "p1-2",
                    success_criteria=("file:P1.md", "file:P2.md"),
                    budget=Budget(max_tool_calls=10, max_attempts=8),
                )
            )
            await asyncio.wait_for(orchestrator.run(), 60)
            store = orchestrator.store
            assert store.get_mission(mission.id).status is MissionStatus.COMPLETED, (
                orchestrator.progress_log
            )
            for task in store.list_tasks(mission.id):
                assert task.budget.max_tool_calls == 5  # shared out, not copied to every Task
                attempt = store.list_attempts(task.id)[0]
                assert store.get_intent_for_subject(attempt.id).config["max_tool_calls"] <= 5
            with store.transaction():
                account = orchestrator.commit.ledger.account(mission_account(mission.id))
            assert account.settled_tool_calls == 4 and account.reserved_tool_calls == 0

    asyncio.run(case())


# ------------------------------------------------------------------ P1-3 durable tool-call count
def test_p1_3_a_restart_settles_the_tool_calls_executed_before_the_crash(tmp_path):
    provider = demo_dynamic_dag_provider(tasks=[_doc("A")], per_attempt={"A": [_doc_script("A")]})
    cfg = config(tmp_path, lease_seconds=0.3)

    async def case():
        async with Orchestrator(cfg, provider, owner="orch-1") as first:
            mission = await first.submit_mission(
                spec(
                    "p1-3",
                    success_criteria=("file:A.md",),
                    budget=Budget(max_tool_calls=10, max_attempts=4),
                )
            )
            first.arm_fault("after_turn_committed", kind="attempt")
            with pytest.raises(InjectedCrash):
                await first.run()
            attempt = first.store.list_attempts(first.store.list_tasks(mission.id)[0].id)[0]
            assert first.store.count_tool_calls(attempt.id) == 2  # recorded as they happened
        await asyncio.sleep(0.35)
        async with Orchestrator(cfg, provider, owner="orch-2") as second:
            await second.run()
            store = second.store
            assert store.get_mission(mission.id).status is MissionStatus.COMPLETED, (
                second.progress_log
            )
            assert not second.assembled.gateway.calls  # this process never saw those calls ...
            with store.transaction():
                account = second.commit.ledger.account(mission_account(mission.id))
            assert account.settled_tool_calls == 2  # ... yet settled them from the durable record
            assert store.count_tool_calls(attempt.id) == 2

    asyncio.run(case())


# ------------------------------------------------------------------ P1-4 submitted turn of an absent pool
def test_p1_4_a_submitted_turn_of_an_absent_pool_does_not_hold_run_and_is_resumed_by_its_pool(
    tmp_path,
):
    small = demo_dynamic_dag_provider(tasks=only("A"), model="fixture-small")
    large = demo_dynamic_dag_provider(tasks=only("A"), model="fixture-large")
    both = {
        "small": RuntimeProfile("small", small, "fixture-small"),
        "large": RuntimeProfile("large", large, "fixture-large", tier=2),
    }
    rules = RoutingRules(default="small", by_role=SERVICES_LARGE)
    cfg = config(tmp_path, lease_seconds=0.3)

    async def case():
        async with Orchestrator(cfg, owner="orch-1", profiles=both, routing=rules) as first:
            mission = await first.submit_mission(
                spec("p1-4", success_criteria=("file:analysis.md",))
            )
            first.arm_fault(
                "after_turn_committed", kind="attempt"
            )  # the turn is done, not yet collected
            with pytest.raises(InjectedCrash):
                await first.run()
            attempt = first.store.list_attempts(first.store.list_tasks(mission.id)[0].id)[0]
            assert first.store.get_intent_for_subject(attempt.id).state == "SUBMITTED"
        await asyncio.sleep(0.35)
        large_only = {"large": RuntimeProfile("large", large, "fixture-large", tier=2)}
        async with Orchestrator(
            cfg, owner="orch-2", profiles=large_only, routing=RoutingRules(default="large")
        ) as second:
            await asyncio.wait_for(
                second.run(), 10
            )  # until idle: returns instead of spinning forever
            assert second.store.get_intent_for_subject(attempt.id).state == "SUBMITTED"
            assert (
                len(second.store.list_attempts(attempt.task_id)) == 1 and large.calls_by_key == {}
            )
        async with Orchestrator(cfg, owner="orch-3", profiles=both, routing=rules) as third:
            await third.run()
            assert third.store.get_mission(mission.id).status is MissionStatus.COMPLETED, (
                third.progress_log
            )
            assert third.store.get_attempt(attempt.id).status is AttemptStatus.COMPLETED
            assert small.calls_by_key["A"] == 3  # the committed turn was collected, never re-run

    asyncio.run(case())


# ------------------------------------------------------------------ P1-5 / P2-10 evidence
def test_p1_5_credential_bearing_artifacts_are_withheld_and_json_is_redacted(tmp_path, monkeypatch):
    provider = demo_dynamic_dag_provider(
        tasks=[_doc("A")],
        per_attempt={"A": [_doc_script("A", content=f"# A\nexample key {FAKE_KEY}\n")]},
    )

    async def case():
        async with Orchestrator(config(tmp_path), provider) as orchestrator:
            mission = await orchestrator.submit_mission(
                spec("p1-5", success_criteria=("file:A.md",))
            )
            await orchestrator.run()
            assert orchestrator.store.get_mission(mission.id).status is MissionStatus.COMPLETED
            monkeypatch.setenv("SH_APIKEY", "fixture-env-secret-0123456789")
            out = write_evidence(
                directory=tmp_path / "e",
                store=orchestrator.store,
                commit=orchestrator.commit,
                mission_id=mission.id,
                baseline={"note": f"key {FAKE_KEY} and fixture-env-secret-0123456789"},
                workspaces_root=orchestrator.assembled.workspaces.root,
                test_report={},
            )
            assert out["withheld_artifacts"] and out["withheld_artifacts"][0]["patterns"] == [
                "api_key_sk"
            ]
            assert out["withheld_artifacts"][0]["copied"] is False
            assert {"file": "baseline.json", "patterns": ["api_key_sk", "env_value"]} in out[
                "redactions"
            ]
            baseline = json.loads((tmp_path / "e" / "baseline.json").read_text())
            assert (
                "<redacted:api_key_sk>" in baseline["note"]
                and "<redacted:env_value>" in baseline["note"]
            )
            for path in (tmp_path / "e").rglob("*"):
                if path.is_file():
                    data = path.read_bytes()
                    assert (
                        FAKE_KEY.encode() not in data
                        and b"fixture-env-secret-0123456789" not in data
                    ), path

    asyncio.run(case())


def test_p2_10_a_planner_package_carrying_a_credential_stops_planning_visibly(tmp_path):
    provider = demo_dynamic_dag_provider(tasks=only("A"))

    async def case():
        async with Orchestrator(config(tmp_path), provider) as orchestrator:
            mission = await orchestrator.submit_mission(
                spec(
                    "ctx",
                    goal=f"实现记录器（示例密钥 {FAKE_KEY}）",
                    success_criteria=("file:analysis.md",),
                )
            )
            await asyncio.wait_for(orchestrator.run(), 30)
            final = orchestrator.store.get_mission(mission.id)
            assert final.status is MissionStatus.FAILED and final.stop_reason == "context_rejected"
            assert provider.calls == 0  # nothing was sent to a model

    asyncio.run(case())


# ------------------------------------------------------------------ P1-6 held reservations
def test_p1_6_a_reservation_held_by_an_unknown_charge_is_listed_and_on_the_timeline(tmp_path):
    from graph_helpers6 import drive_to_running, graph_service

    service, mission, t = graph_service(tmp_path)
    attempt = drive_to_running(service, t["A"])
    with service.store.transaction():
        service.ledger.import_usage(
            subject_id=attempt.id,
            mission_id=mission.id,
            facts=[UsageFact("u-1", 10, 5, None, unknown=True)],
        )
        report = service.ledger.costs_report(mission.id)
    assert [r["subject_id"] for r in report["held_reservations"]] == [attempt.id]
    first = service.record_reservation_held(
        attempt.id, mission.id, task_id=t["A"].id, reason="unknown_usage"
    )
    again = service.record_reservation_held(
        attempt.id, mission.id, task_id=t["A"].id, reason="unknown_usage"
    )
    assert first.id == again.id and service.store.count_events(mission.id, "ReservationHeld") == 1
    assert first.payload["reserved_tokens"] == 4_000


# ------------------------------------------------------------------ P2-1 / P2-3 routing details
def test_p2_1_turn_errors_are_classified_by_exact_codes_only():
    assert (
        classify_turn_error({"raw_failures": [{"error_code": "provider_server_error"}]})
        == "provider_unavailable"
    )
    assert classify_turn_error({"error_code": "provider_protocol_error"}) == "provider_error"
    assert (
        classify_turn_error({"error_code": "provider_empty_response"}) == "other"
    )  # already retried in the turn
    assert (
        classify_turn_error({"error_code": "provider_cancelled"}) == "other"
    )  # our own cancel is not ill health
    assert (
        classify_turn_error({"message": "see provider_server_error in the docs"}) == "other"
    )  # free text is not a code


def test_p2_3_a_fallback_that_is_cooling_down_too_is_not_used():
    class _Stub:
        async def invoke(self, request, *, cancel):  # type: ignore[no-untyped-def]
            raise AssertionError

    profiles = {n: RuntimeProfile(n, _Stub(), f"m-{n}") for n in ("small", "large")}
    router = ModelRouter(profiles, RoutingRules(default="small", fallback={"small": "large"}))
    with pytest.raises(RoutingUnavailable) as unavailable:
        router.route(role="worker", unavailable_until={"small": 100.0, "large": 90.0}, now=50.0)
    assert unavailable.value.profile_id == "large"
