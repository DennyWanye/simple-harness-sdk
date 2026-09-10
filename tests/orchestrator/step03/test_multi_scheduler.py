# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501

"""Step 3 · S3-04 / S3-07: two Orchestrator instances over the same two libraries
(orchestrator.db + execution.db) — one owner per Attempt, one SDK Agent / Turn per
Attempt, the Mission completes exactly once; a lost orchestration lease is taken over
without re-running finished work, an executor that vanished is LOST and retried."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest
from test_static_dag_closure import CALLS, config, spec, tasks_by_key

from agent_orchestrator.contracts import AttemptStatus, MissionStatus, TaskStatus
from agent_orchestrator.orchestrator.event_handler import Orchestrator
from agent_orchestrator.storage.store import InjectedCrash
from agent_orchestrator.testing.fixtures import demo_static_dag_provider


def _sdk(evidence_root: Path) -> dict[str, object]:
    connection = sqlite3.connect(evidence_root / "execution.db")
    agents = connection.execute("SELECT COUNT(*) FROM base_agent_bindings_v1").fetchone()[0]
    turns = connection.execute(
        "SELECT agent_id, COUNT(*) FROM base_agent_turns_v1 GROUP BY agent_id"
    ).fetchall()
    connection.close()
    return {"agents": agents, "turns_by_agent": dict(turns)}


def test_s3_04_two_orchestrators_share_the_work_without_double_execution(tmp_path):
    provider = demo_static_dag_provider()
    evidence = Path(tmp_path) / "evidence"

    async def case():
        async with (
            Orchestrator(config(tmp_path), provider, owner="orch-1") as first,
            Orchestrator(config(tmp_path), provider, owner="orch-2") as second,
        ):
            mission = await first.submit_mission(spec("s3-04"))
            await asyncio.gather(first.run(), second.run())
            store = first.store
            final = store.get_mission(mission.id)
            assert final.status is MissionStatus.COMPLETED, (
                first.progress_log,
                second.progress_log,
            )
            assert store.count_events(mission.id, "MissionCompleted") == 1
            by_key = tasks_by_key(store, mission.id)
            owners = {}
            for key, task in by_key.items():
                attempts = store.list_attempts(task.id)
                assert [a.status for a in attempts] == [AttemptStatus.COMPLETED]
                assert attempts[0].lease_owner in {"orch-1", "orch-2"}  # exactly one owner
                owners[key] = attempts[0].lease_owner
                intent = store.get_intent_for_subject(attempts[0].id)
                assert intent.state == "SETTLED" and intent.agent_id == attempts[0].agent_id
            assert provider.calls_by_key == CALLS  # every Worker script ran exactly once
            sdk = _sdk(evidence)
            assert sdk["agents"] == 6  # planner + 5 workers (no critics in this policy)
            assert set(sdk["turns_by_agent"].values()) == {1}  # one turn per Agent
            # every Attempt was claimed exactly once (one owner, never a double dispatch)
            claimed = [e for e in store.list_events(mission.id) if e.type == "AttemptClaimed"]
            assert sorted(e.attempt_id for e in claimed) == sorted(
                a.id for t in by_key.values() for a in store.list_attempts(t.id)
            )
            assert store.count_events(mission.id, "AttemptStarted") == 5
            return owners

    asyncio.run(case())


def _crash_after_b_submitted(tmp_path, provider, lease):
    """orch-1 finishes A, then dies right after submitting B's turn (a queued SDK turn
    with an orchestration lease held by a dead owner).  Returns (mission_id, b_attempt)."""

    async def phase():
        async with Orchestrator(config(tmp_path, **lease), provider, owner="orch-1") as first:
            mission = await first.submit_mission(spec("s3-07"))
            first.arm_fault("after_submit", kind="attempt", skip=1)  # A passes, B crashes
            with pytest.raises(InjectedCrash):
                await first.run()
            store = first.store
            by_key = tasks_by_key(store, mission.id)
            assert by_key["A"].status is TaskStatus.COMPLETED
            b_attempt = store.list_attempts(by_key["B"].id)[0]
            # the SDK turn is submitted; the receipt was not recorded yet (S2-04b shape)
            assert b_attempt.status is AttemptStatus.CLAIMED and b_attempt.lease_owner == "orch-1"
            assert b_attempt.turn_id is not None
            return mission.id, b_attempt, dict(provider.calls_by_key)

    return asyncio.run(phase())


def test_s3_07a_new_owner_takes_over_the_in_flight_turn_without_rerunning(tmp_path):
    provider = demo_static_dag_provider()
    lease = dict(lease_seconds=0.6, sdk_lease_ttl_seconds=0.3)
    mission_id, b_attempt, calls_at_crash = _crash_after_b_submitted(tmp_path, provider, lease)
    assert calls_at_crash["A"] == CALLS["A"]

    async def takeover():
        await asyncio.sleep(0.7)  # orch-1's orchestration lease (0.6 s) lapses (§17.6)
        async with Orchestrator(config(tmp_path, **lease), provider, owner="orch-2") as second:
            await second.run()
            store = second.store
            final = store.get_mission(mission_id)
            assert final.status is MissionStatus.COMPLETED, second.progress_log
            by_key = tasks_by_key(store, mission_id)
            # A was not re-run; B is the *same* Attempt, finished under the new owner
            assert provider.calls_by_key == CALLS
            b_attempts = store.list_attempts(by_key["B"].id)
            assert [a.id for a in b_attempts] == [b_attempt.id]
            assert b_attempts[0].status is AttemptStatus.COMPLETED
            assert b_attempts[0].lease_owner == "orch-2"
            assert (
                b_attempts[0].turn_id == b_attempt.turn_id
            )  # the SDK turn was resumed, not replaced
            sdk = _sdk(Path(tmp_path) / "evidence")
            assert set(sdk["turns_by_agent"].values()) == {1}
            assert store.count_events(mission_id, "MissionCompleted") == 1
            assert store.count_events(mission_id, "AttemptLost") == 0

    asyncio.run(takeover())


def test_s3_07b_vanished_executor_is_lost_and_retried_without_rerunning_finished_work(tmp_path):
    provider = demo_static_dag_provider()
    lease = dict(lease_seconds=0.6, sdk_lease_ttl_seconds=0.3)
    mission_id, b_attempt, _ = _crash_after_b_submitted(tmp_path, provider, lease)
    # B's executor vanishes: its SDK records belong to a foreign scope and its turn is not
    # resumable (the "executor invisible" case of S3-07)
    connection = sqlite3.connect(Path(tmp_path) / "evidence" / "execution.db")
    connection.execute(
        "UPDATE base_agent_bindings_v1 SET owner_scope = 'foreign' WHERE agent_id = ?",
        (b_attempt.agent_id,),
    )
    connection.execute(
        "UPDATE base_agent_turns_v1 SET phase = 'failed' WHERE agent_id = ?",
        (b_attempt.agent_id,),
    )
    connection.commit()
    connection.close()

    async def takeover():
        await asyncio.sleep(0.7)
        async with Orchestrator(config(tmp_path, **lease), provider, owner="orch-2") as second:
            await second.run()
            store = second.store
            final = store.get_mission(mission_id)
            assert final.status is MissionStatus.COMPLETED, second.progress_log
            by_key = tasks_by_key(store, mission_id)
            b_attempts = store.list_attempts(by_key["B"].id)
            assert [a.status for a in b_attempts] == [AttemptStatus.LOST, AttemptStatus.COMPLETED]
            assert b_attempts[0].id == b_attempt.id
            assert b_attempts[0].failure["reason"] in {
                "executor_turn_missing",
                "executor_agent_missing",
            }
            assert b_attempts[1].lease_owner == "orch-2" and b_attempts[1].retry_of == b_attempt.id
            assert (
                provider.calls_by_key == CALLS
            )  # A once; B's script ran once (by the new Attempt)
            assert store.count_events(mission_id, "AttemptLost") == 1
            assert store.count_events(mission_id, "MissionCompleted") == 1
            with store.transaction():
                lost = second.commit.ledger.reservation(b_attempt.id)
            assert lost["state"] == "SETTLED"  # the lost Attempt's reservation was released

    asyncio.run(takeover())
