# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501

"""Step 6 · S6-03 / S6-06 / S6-08 (D6-4 / D6-5): physical model routing.  A runtime
profile is one provider + model + execution library; the route is frozen in the
dispatch intent and proven by the provider's echo; failures climb §9.3's ladder to a
stronger profile with the earlier Attempt and its cost kept; an unavailable service is
replaced by a fallback or waited for (bounded); a restart never hands an Attempt to
another pool."""

from __future__ import annotations

import asyncio

import pytest
from helpers_step06 import config, events_of, spec

from agent_orchestrator.contracts import Attempt, AttemptStatus, Budget, MissionStatus
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
    recorder_scripts,
)


def _impl_task(**overrides):
    task = _recorder_task(
        "A",
        "实现 recorder.py 并通过 tests/test_recorder.py（独立任务）",
        [],
        ["pytest:tests/test_recorder.py"],
        3.0,
        ["recorder.py"],
        policy=["format_check", "rule_check", "code_test"],
    )
    task.update(overrides)
    return task


def _wrong():
    return _write_files_then(
        {"recorder.py": "def parse_line(line):\n    return {}\n"},
        test_path="tests/test_recorder.py",
        summary="实现完成",
        claim="tests/test_recorder.py 通过",
    )


def _doc_task():
    return _recorder_task(
        "A", "分析 spec/INPUT.md 并写出 analysis.md", [], ["file:analysis.md"], 3.0, ["analysis.md"]
    )


class _Stub:
    async def invoke(self, request, *, cancel):  # type: ignore[no-untyped-def]
        raise AssertionError("never called")


def _profiles(*names, tiers=None):
    return {
        n: RuntimeProfile(n, _Stub(), f"model-{n}", tier=(tiers or {}).get(n, i))
        for i, n in enumerate(names)
    }


def _attempt(profile, reason, ordinal=1, error_kind=None):
    failure = (
        None
        if reason is None
        else {"reason": reason, **({} if error_kind is None else {"error_kind": error_kind})}
    )
    return Attempt(
        id=f"t:attempt-{ordinal}",
        task_id="t",
        mission_id="m",
        role="worker",
        model=f"model-{profile}",
        prompt_version="w",
        context_version="c",
        budget_reserved=Budget(),
        lease_owner=None,
        lease_expires_at=None,
        status=AttemptStatus.RETRY_WAIT if reason else AttemptStatus.COMPLETED,
        retry_of=None,
        created_at=0.0,
        version=1,
        ordinal=ordinal,
        creation_key="k",
        input_id="i",
        runtime_profile_id=profile,
        failure=failure,
    )


# ------------------------------------------------------------------ unit: the router
def test_routing_by_role_then_task_kind_then_default_and_rule_validation():
    router = ModelRouter(
        _profiles("small", "large"),
        RoutingRules(
            default="small", by_role={"planner": "large"}, by_task_kind={"proof": "large"}
        ),
    )
    assert router.route(role="planner", task_kind="code").profile_id == "large"
    assert router.route(role="worker", task_kind="proof").reason == "by_task_kind:proof"
    decision = router.route(role="worker", task_kind="code")
    assert (decision.profile_id, decision.model, decision.reason) == (
        "small",
        "model-small",
        "default",
    )
    with pytest.raises(ValueError):
        ModelRouter(_profiles("small"), RoutingRules(default="small", escalate={"small": "huge"}))
    with pytest.raises(ValueError):
        ModelRouter({}, RoutingRules())


def test_the_escalation_ladder_climbs_after_failures_on_the_current_rung_only():
    router = ModelRouter(
        _profiles("small", "medium", "large"),
        RoutingRules(
            default="small",
            escalate={"small": "medium", "medium": "large"},
            escalate_after_failures=1,
        ),
    )
    assert router.route(role="worker", previous_attempts=[]).profile_id == "small"
    one = router.route(role="worker", previous_attempts=[_attempt("small", "verification_failed")])
    assert (
        one.profile_id == "medium"
        and one.escalated_from == "small"
        and one.reason.startswith("escalate:small->medium")
    )
    two = router.route(
        role="worker",
        previous_attempts=[
            _attempt("small", "verification_failed"),
            _attempt("medium", "turn_failed", 2),
        ],
    )
    assert two.profile_id == "large" and two.escalated_from == "medium"
    # an unavailable provider is a health matter, never a reason to escalate (review P1-8)
    down = router.route(
        role="worker",
        previous_attempts=[_attempt("small", "turn_failed", error_kind="provider_unavailable")],
    )
    assert down.profile_id == "small"
    # a completed Attempt or a non-failure reason does not count
    fine = router.route(
        role="worker",
        previous_attempts=[_attempt("small", None), _attempt("small", "outcome_blocked", 2)],
    )
    assert fine.profile_id == "small"


def test_an_unavailable_profile_falls_back_or_makes_the_caller_wait():
    router = ModelRouter(
        _profiles("small", "large"), RoutingRules(default="small", fallback={"small": "large"})
    )
    decision = router.route(role="worker", unavailable_until={"small": 100.0}, now=50.0)
    assert decision.profile_id == "large" and decision.fallback_from == "small"
    assert (
        router.route(role="worker", unavailable_until={"small": 100.0}, now=150.0).profile_id
        == "small"
    )
    lonely = ModelRouter(_profiles("small"), RoutingRules(default="small"))
    with pytest.raises(RoutingUnavailable) as unavailable:
        lonely.route(role="worker", unavailable_until={"small": 100.0}, now=50.0)
    assert unavailable.value.profile_id == "small" and unavailable.value.until == 100.0


def test_turn_errors_are_classified_by_the_sdk_error_vocabulary():
    assert (
        classify_turn_error({"code": "provider_server_error", "message": "503"})
        == "provider_unavailable"
    )
    assert classify_turn_error({"kind": "provider_rate_limited"}) == "provider_unavailable"
    assert (
        classify_turn_error({"code": "provider_protocol_error", "detail": "tool_parse"})
        == "provider_error"
    )
    assert (
        classify_turn_error({"code": "turn_deadline"}) == "other"
        and classify_turn_error(None) == "other"
    )


# ------------------------------------------------------------------ S6-03
def test_s6_03_a_small_model_failure_escalates_to_the_large_model_with_the_first_attempt_kept(
    tmp_path,
):
    small = demo_dynamic_dag_provider(
        tasks=[_impl_task()], per_attempt={"A": [_wrong()]}, model="fixture-small"
    )
    large = demo_dynamic_dag_provider(
        tasks=[_impl_task()], per_attempt={"A": [recorder_scripts()["B2"]]}, model="fixture-large"
    )
    profiles = {
        "small": RuntimeProfile("small", small, "fixture-small", tier=1),
        "large": RuntimeProfile("large", large, "fixture-large", tier=2),
    }
    rules = RoutingRules(
        default="small",
        by_role={"planner": "large", "manager": "large", "critic": "large"},
        escalate={"small": "large"},
        escalate_after_failures=1,
    )

    async def case():
        async with Orchestrator(
            config(tmp_path, manager_after_failures=10), profiles=profiles, routing=rules
        ) as orchestrator:
            mission = await orchestrator.submit_mission(
                spec("s6-03", success_criteria=("pytest:tests/test_recorder.py",))
            )
            await orchestrator.run()
            store = orchestrator.store
            assert store.get_mission(mission.id).status is MissionStatus.COMPLETED, (
                orchestrator.progress_log
            )
            task = store.list_tasks(mission.id)[0]
            first, second = store.list_attempts(task.id)
            assert (first.runtime_profile_id, first.model, first.status) == (
                "small",
                "fixture-small",
                AttemptStatus.RETRY_WAIT,
            )
            assert (second.runtime_profile_id, second.model, second.status) == (
                "large",
                "fixture-large",
                AttemptStatus.COMPLETED,
            )
            assert second.retry_of == first.id  # the earlier Attempt is history, not relabelled
            routed = events_of(store, mission.id, "ModelRouted")
            assert [e.payload["runtime_profile_id"] for e in routed] == ["small", "large"]
            assert routed[1].payload["escalated_from"] == "small" and routed[1].payload[
                "reason"
            ].startswith("escalate:small->large")
            # the physical route is proven by the provider's echo in each pool's own library
            small_intent = store.get_intent_for_subject(first.id)
            large_intent = store.get_intent_for_subject(second.id)
            assert (
                small_intent.config["model"] == "fixture-small"
                and large_intent.config["model"] == "fixture-large"
            )
            assert orchestrator.assembled.pools["small"].bridge.echoed_models(
                agent_id=small_intent.agent_id
            ) == {"fixture-small"}
            assert orchestrator.assembled.pools["large"].bridge.echoed_models(
                agent_id=large_intent.agent_id
            ) == {"fixture-large"}
            assert small_intent.config["agent_config"]["model_profile_ref"] == "small"
            # both Attempts' costs are on the books
            report = orchestrator.commit.ledger.costs_report(mission.id)
            subjects = {u["subject_id"] for u in report["usage"]}
            assert first.id in subjects and second.id in subjects
            assert all(r["state"] == "SETTLED" for r in report["reservations"])
            # the Planner ran in the large pool (by role)
            planner = store.get_intent_for_subject(f"{mission.id}:planner:1")
            assert (
                planner.config["runtime_profile_id"] == "large"
                and planner.config["agent_config"]["model_profile_ref"] == "large"
            )
            assert (tmp_path / "evidence" / "execution-small.db").is_file() and (
                tmp_path / "evidence" / "execution-large.db"
            ).is_file()

    asyncio.run(case())


# ------------------------------------------------------------------ S6-06
def test_s6_06_an_unavailable_service_falls_back_to_the_other_profile_and_is_recorded(tmp_path):
    down = UnavailableProvider(model="fixture-down")
    large = demo_dynamic_dag_provider(tasks=[_doc_task()], model="fixture-large")
    profiles = {
        "small": RuntimeProfile("small", down, "fixture-down"),
        "large": RuntimeProfile("large", large, "fixture-large", tier=2),
    }
    rules = RoutingRules(
        default="small",
        by_role={"planner": "large", "critic": "large", "manager": "large"},
        fallback={"small": "large"},
    )

    async def case():
        async with Orchestrator(
            config(
                tmp_path,
                profile_failure_threshold=2,
                profile_cooldown_seconds=30.0,
                manager_after_failures=10,
            ),
            profiles=profiles,
            routing=rules,
        ) as orchestrator:
            mission = await orchestrator.submit_mission(
                spec("s6-06a", success_criteria=("file:analysis.md",))
            )
            await orchestrator.run()
            store = orchestrator.store
            assert store.get_mission(mission.id).status is MissionStatus.COMPLETED, (
                orchestrator.progress_log
            )
            attempts = store.list_attempts(store.list_tasks(mission.id)[0].id)
            assert [a.runtime_profile_id for a in attempts] == ["small", "small", "large"]
            assert all(a.failure["error_kind"] == "provider_unavailable" for a in attempts[:2])
            unavailable = events_of(store, mission.id, "RuntimeProfileUnavailable")
            assert len(unavailable) == 1 and unavailable[0].payload["profile_id"] == "small"
            assert (
                unavailable[0].payload["reason"] == "provider_unavailable"
                and unavailable[0].payload["trips"] == 1
            )
            routed = events_of(store, mission.id, "ModelRouted")
            assert routed[-1].payload["fallback_from"] == "small" and routed[-1].payload[
                "reason"
            ].startswith("fallback:unavailable")
            health = orchestrator.commit.profile_health()["small"]
            assert health["unavailable_until"] is not None and health["trips"] == 1
            assert (
                down.calls == 2
            )  # the cooling-down profile was not probed again while unavailable

    asyncio.run(case())


def test_s6_06_without_a_fallback_the_task_waits_a_bounded_time_while_other_missions_continue(
    tmp_path,
):
    down = UnavailableProvider(model="fixture-down")
    large = demo_dynamic_dag_provider(tasks=[_doc_task()], model="fixture-large")
    large.scripts["planner"] = large.scripts["planner"] * 2
    large.per_attempt["A"] = [recorder_scripts()["A"], recorder_scripts()["A"]]
    profiles = {
        "small": RuntimeProfile("small", down, "fixture-down"),
        "large": RuntimeProfile("large", large, "fixture-large", tier=2),
    }
    rules = RoutingRules(
        default="small",
        by_role={"planner": "large", "critic": "large", "manager": "large"},
        by_task_kind={"docs": "large"},
    )

    async def case():
        async with Orchestrator(
            config(
                tmp_path,
                profile_failure_threshold=2,
                profile_cooldown_seconds=30.0,  # longer than the wait: the Task cannot outlast it
                profile_wait_seconds=0.5,
                manager_after_failures=10,
            ),
            profiles=profiles,
            routing=rules,
            poll_interval=0.02,
        ) as orchestrator:
            stuck = await orchestrator.submit_mission(
                spec("s6-06b", success_criteria=("file:analysis.md",))
            )
            other = await orchestrator.submit_mission(
                spec("s6-06c", success_criteria=("file:analysis.md",), task_kind="docs")
            )
            await orchestrator.run()
            store = orchestrator.store
            assert store.get_mission(other.id).status is MissionStatus.COMPLETED, (
                orchestrator.progress_log
            )
            final = store.get_mission(stuck.id)
            assert (
                final.status is MissionStatus.FAILED and final.stop_reason == "runtime_unavailable"
            )
            assert final.final_report["detail"]["profile_id"] == "small"
            assert final.final_report["detail"]["waited_seconds"] >= 0.5
            attempts = store.list_attempts(store.list_tasks(stuck.id)[0].id)
            assert len(attempts) == 2 and down.calls == 2  # no probing during the cooldown
            assert events_of(store, stuck.id, "RuntimeProfileUnavailable")
            assert not orchestrator._deferred

    asyncio.run(case())


# ------------------------------------------------------------------ S6-08
def test_s6_08_after_a_restart_an_attempt_stays_with_its_own_pool_and_is_never_taken_by_another_model(
    tmp_path,
):
    small = demo_dynamic_dag_provider(tasks=[_doc_task()], model="fixture-small")
    large = demo_dynamic_dag_provider(tasks=[_doc_task()], model="fixture-large")
    both = {
        "small": RuntimeProfile("small", small, "fixture-small"),
        "large": RuntimeProfile("large", large, "fixture-large", tier=2),
    }
    rules = RoutingRules(
        default="small", by_role={"planner": "large", "critic": "large", "manager": "large"}
    )
    cfg = config(tmp_path, lease_seconds=0.3)

    async def case():
        async with Orchestrator(cfg, owner="orch-1", profiles=both, routing=rules) as first:
            mission = await first.submit_mission(
                spec("s6-08", success_criteria=("file:analysis.md",))
            )
            # the crash lands after the Agent was created in the small pool's library and before
            # any input was submitted: no outbound effect is in flight (a crash mid tool call is
            # an UNKNOWN effect and stays blocked by design, S2-08)
            first.arm_fault("after_agent_created", kind="attempt")
            with pytest.raises(InjectedCrash):
                await first.run()
            attempt = first.store.list_attempts(first.store.list_tasks(mission.id)[0].id)[0]
            intent = first.store.get_intent_for_subject(attempt.id)
            assert (
                attempt.runtime_profile_id == "small"
                and intent.config["runtime_profile_id"] == "small"
            )
            assert intent.state == "CLAIMED" and intent.config["model"] == "fixture-small"
        await asyncio.sleep(0.35)  # the dead owner's lease lapses
        # a restart that runs only the large pool must not touch the small pool's Attempt
        large_only = {"large": RuntimeProfile("large", large, "fixture-large", tier=2)}
        async with Orchestrator(
            cfg, owner="orch-2", profiles=large_only, routing=RoutingRules(default="large")
        ) as second:
            await second.run(max_cycles=4, until_idle=False)
            store = second.store
            still = store.get_attempt(attempt.id)
            assert still.status is attempt.status and still.runtime_profile_id == "small"
            untouched = store.get_intent_for_subject(attempt.id)
            assert (
                untouched.state == "CLAIMED" and untouched.lease_owner == "orch-1"
            )  # not claimed by orch-2
            assert (
                len(store.list_attempts(still.task_id)) == 1
            )  # no substitute Attempt on the large pool
            missing = events_of(store, mission.id, "RuntimeProfileUnavailable")
            assert len(missing) == 1
            assert (
                missing[0].payload["profile_id"] == "small"
                and missing[0].payload["reason"] == "not_configured"
            )
            assert large.calls_by_key == {}  # the large model never ran the Worker
        # the right pool picks the Attempt up again: the same creation key, the same Agent
        async with Orchestrator(cfg, owner="orch-3", profiles=both, routing=rules) as third:
            await third.run()
            store = third.store
            assert store.get_mission(mission.id).status is MissionStatus.COMPLETED, (
                third.progress_log
            )
            done = store.get_attempt(attempt.id)
            assert done.status is AttemptStatus.COMPLETED and done.runtime_profile_id == "small"
            final = store.get_intent_for_subject(attempt.id)
            assert third.assembled.pools["small"].bridge.echoed_models(agent_id=final.agent_id) == {
                "fixture-small"
            }
            large_view = await third.assembled.pools["large"].bridge.liveness(
                agent_id=final.agent_id, turn_id=final.expected_turn_id
            )
            assert not large_view.exists  # the large pool's library never held this Agent
            assert (
                small.calls_by_key["A"] == 3
            )  # one script's worth: list, write, envelope; never re-run
            assert large.calls_by_key == {}

    asyncio.run(case())
