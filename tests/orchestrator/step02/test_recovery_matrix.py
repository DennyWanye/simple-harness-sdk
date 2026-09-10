# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501

"""Step 2 · S2-03 / S2-04 / S2-05 / S2-08: crashes at the cross-database instants and
replays never produce a second execution, a second delivery or a second charge; an
UNKNOWN provider outcome keeps the Attempt blocked with its reservation held."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest
from fixtures_provider import (
    RoleScriptedProvider,
    UnknownAfterHandoff,
    critic_step,
    proposal_step,
)
from test_single_task_closure import GOOD, PROPOSAL, config, spec, worker_script

from agent_orchestrator.contracts import AttemptStatus, MissionStatus, TaskStatus
from agent_orchestrator.governance.budgets import UsageFact
from agent_orchestrator.orchestrator.event_handler import Orchestrator
from agent_orchestrator.storage.store import InjectedCrash


def _provider():
    return RoleScriptedProvider(
        {
            "planner": [proposal_step(PROPOSAL)],
            "worker": worker_script(GOOD),
            "critic": [critic_step(verdict="PASS", criteria_met=True)],
        }
    )


def _sdk_counts(evidence_root: Path) -> dict[str, int]:
    connection = sqlite3.connect(evidence_root / "execution.db")
    agents = connection.execute("SELECT COUNT(*) FROM base_agent_bindings_v1").fetchone()[0]
    turns = connection.execute("SELECT COUNT(*) FROM base_agent_turns_v1").fetchone()[0]
    invocations = connection.execute("SELECT COUNT(*) FROM provider_invocations").fetchone()[0]
    connection.close()
    return {"agents": agents, "turns": turns, "invocations": invocations}


def _crash_then_recover(tmp_path, point: str, kind: str = "attempt"):
    provider = _provider()
    evidence = Path(tmp_path) / "evidence"

    async def case():
        # short business lease so the "dead" first process's claim expires quickly
        async with Orchestrator(
            config(tmp_path, lease_seconds=0.3), provider, owner="orch-1"
        ) as first:
            mission = await first.submit_mission(spec(f"m-{point}"))
            first.arm_fault(point, kind=kind)
            with pytest.raises(InjectedCrash):
                await first.run()
            assert first.store.fired == [f"{point}:{kind}"]
            crashed_state = first.store.snapshot(mission.id)
            worker_calls_at_crash = provider.by_role.get("worker", 0)
        await asyncio.sleep(0.35)  # the crashed owner's lease lapses (§17.6)
        # "process restart": a new orchestrator on the same two libraries
        async with Orchestrator(
            config(tmp_path, lease_seconds=0.3), provider, owner="orch-2"
        ) as second:
            await second.run()
            store = second.store
            final = store.get_mission(mission.id)
            assert final.status is MissionStatus.COMPLETED, second.progress_log
            task = store.list_tasks(mission.id)[0]
            attempts = store.list_attempts(task.id)
            assert len(attempts) == 1 and attempts[0].status is AttemptStatus.COMPLETED
            return (
                crashed_state,
                worker_calls_at_crash,
                provider,
                _sdk_counts(evidence),
                store.snapshot(mission.id),
            )

    return asyncio.run(case())


def test_s2_04_crash_after_agent_created_replays_the_same_agent_and_turn(tmp_path):
    crashed, worker_calls, provider, sdk, final = _crash_then_recover(
        tmp_path, "after_agent_created"
    )
    intent = final["intents"][0]
    assert intent["state"] == "SETTLED" and intent["replays"] >= 1
    # exactly one worker Agent + one planner + one critic in the SDK, one turn each
    assert sdk["agents"] == 3 and sdk["turns"] == 3
    # the worker script ran once: 5 model calls (4 tools + envelope), none repeated
    assert provider.by_role == {"planner": 1, "worker": 5, "critic": 1}
    attempt = final["attempts"][0]
    assert (
        attempt["agent_id"] == intent["agent_id"]
        and attempt["turn_id"] == intent["expected_turn_id"]
    )


def test_s2_04p_crash_during_planner_dispatch_replays_the_planner_once(tmp_path):
    crashed, worker_calls, provider, sdk, final = _crash_then_recover(
        tmp_path, "after_agent_created", kind="plan"
    )
    assert provider.by_role == {"planner": 1, "worker": 5, "critic": 1}
    assert sdk["agents"] == 3 and sdk["turns"] == 3


def test_s2_04b_crash_after_submit_before_receipt_replays_without_a_second_turn(tmp_path):
    crashed, worker_calls, provider, sdk, final = _crash_then_recover(tmp_path, "after_submit")
    assert sdk["agents"] == 3 and sdk["turns"] == 3
    assert provider.by_role["worker"] == 5
    assert final["intents"][0]["receipt"]["turn_id"] == final["attempts"][0]["turn_id"]


def test_s2_05_crash_after_result_submitted_only_resumes_verification(tmp_path):
    crashed, worker_calls, provider, sdk, final = _crash_then_recover(
        tmp_path, "after_result_submitted"
    )
    assert worker_calls == 5 and provider.by_role["worker"] == 5  # the Worker never re-ran
    assert crashed["results"][0]["verification_state"] == "PENDING"
    assert final["results"][0]["verdict"] == "PASS"
    assert sdk["turns"] == 3


def test_s2_05b_crash_after_turn_committed_before_result_recorded(tmp_path):
    crashed, worker_calls, provider, sdk, final = _crash_then_recover(
        tmp_path, "after_turn_committed"
    )
    assert provider.by_role["worker"] == 5 and sdk["turns"] == 3
    assert final["results"][0]["verdict"] == "PASS"


def test_s2_05c_crash_after_a_verification_layer_passed(tmp_path):
    crashed, worker_calls, provider, sdk, final = _crash_then_recover(tmp_path, "after_layer_pass")
    assert provider.by_role["worker"] == 5
    layers = {v["layer"]: v["status"] for v in final["results"][0]["verifications"]}
    assert layers["format_check"] == "PASS" and layers["code_test"] == "PASS"
    # the critic ran once per verification pass; a re-verification after the crash may call it again
    assert provider.by_role["critic"] in (1, 2)


def test_s2_03_replays_do_not_duplicate(tmp_path):
    provider = _provider()

    async def case():
        async with Orchestrator(config(tmp_path), provider) as orchestrator:
            mission = await orchestrator.submit_mission(spec("m3b"))
            await orchestrator.run()
            store, commit = orchestrator.store, orchestrator.commit
            task = store.list_tasks(mission.id)[0]
            attempt = store.list_attempts(task.id)[0]
            stored = store.find_result_for_attempt(attempt.id)
            events_before = store.count_events(mission.id)
            # same usage fact imported twice → once
            with store.transaction():
                usage_before = commit.ledger.usage_for(attempt.id)
            assert (
                commit.import_usage(
                    attempt.id, mission.id, [UsageFact("provider-invocation:dup", 1, 1, None)]
                )
                == 1
            )
            assert (
                commit.import_usage(
                    attempt.id, mission.id, [UsageFact("provider-invocation:dup", 1, 1, None)]
                )
                == 0
            )
            # same artifact registered twice → one row
            artifact = store.get_artifact(task.accepted_artifacts[0])
            store.upsert_artifact(artifact)
            assert len(store.list_artifacts(attempt.id)) == len(
                {a.id for a in store.list_artifacts(attempt.id)}
            )
            # same verification layer recorded twice → one row, one event
            commit.record_verification_layer(
                stored.envelope.id, layer="code_test", status="PASS", detail={"summary": "again"}
            )
            assert (
                len(
                    [
                        v
                        for v in store.list_verifications(stored.envelope.id)
                        if v["layer"] == "code_test"
                    ]
                )
                == 1
            )
            # same result delivered twice → same stored result, no new event
            again = commit.record_result(
                attempt.id,
                envelope=stored.envelope,
                turn_id=stored.turn_id,
                artifacts=[],
                usage_refs=(),
            )
            assert again.envelope.id == stored.envelope.id
            # a second run() on a finished Mission changes nothing
            await orchestrator.run()
            assert store.count_events(mission.id) == events_before
            assert provider.by_role == {"planner": 1, "worker": 5, "critic": 1}
            assert usage_before[0] > 0

    asyncio.run(case())


def test_s2_08_unknown_provider_outcome_stays_blocked_with_reservation_held(tmp_path):
    provider = RoleScriptedProvider(
        {
            "planner": [proposal_step(PROPOSAL)],
            "worker": [("workspace_list", {}), UnknownAfterHandoff("lost")],
            "critic": [],
        }
    )

    async def case():
        async with Orchestrator(
            config(tmp_path, lease_seconds=0.2), provider, poll_interval=0.02
        ) as orchestrator:
            mission = await orchestrator.submit_mission(spec("m8"))
            await orchestrator.run(max_cycles=40, until_idle=False)
            for _ in range(40):
                await orchestrator.run(max_cycles=1, until_idle=False)
                await asyncio.sleep(0.02)
            store = orchestrator.store
            task = store.list_tasks(mission.id)[0]
            attempt = store.list_attempts(task.id)[0]
            assert attempt.status is AttemptStatus.RUNNING, orchestrator.progress_log
            assert store.get_task(task.id).status is TaskStatus.ACTIVE
            liveness = await orchestrator.bridge.liveness(
                agent_id=attempt.agent_id, turn_id=attempt.turn_id
            )
            assert liveness.alive and liveness.blocked
            with store.transaction():
                reservation = orchestrator.commit.ledger.reservation(attempt.id)
                account = orchestrator.commit.ledger.account(f"budget:{task.id}")
            assert reservation["state"] == "RESERVED" and account.reserved_tokens > 0
            assert account.settled_cost_micros == 0 and account.settled_tokens == 0
            heartbeats = store.count_events(mission.id, "HeartbeatReceived")
            assert heartbeats >= 1
            assert store.count_events(mission.id, "AttemptLost") == 0
            assert store.get_mission(mission.id).status is MissionStatus.ACTIVE

    asyncio.run(case())
