# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""Actual SDK workers and code verification, with deterministic completion gates.

No acceptance/state/ledger is mocked. Private cycle stepping stops the real loop
at a durable pre-judgment boundary; it does not fabricate a Result or a verdict.
The parent must run test-only first: late_lease must fail on the old runtime.
"""

from __future__ import annotations

import asyncio
import hashlib

import pytest

from agent_orchestrator.contracts import AttemptStatus, Budget, MissionStatus, TaskStatus
from agent_orchestrator.orchestrator.commit_service import (
    CommitRejected,
    MissionSpec,
    mission_account,
    task_account,
)
from agent_orchestrator.orchestrator.event_handler import Orchestrator
from agent_orchestrator.runtime.assembly import OrchestratorConfig
from agent_orchestrator.testing.fixtures import (
    RECORDER_IMPL,
    RECORDER_SEED,
    RECORDER_SPEC,
    _recorder_task,
    _write_files_then,
    demo_dynamic_dag_provider,
)
from agent_orchestrator.verification import verifier_router

WRONG = "def parse_line(line):\n    return {}\n"


def _sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def _script(code):
    return _write_files_then(
        {"recorder.py": code},
        test_path="tests/test_recorder.py",
        summary="implementation candidate",
        claim="tests/test_recorder.py passes",
    )


@pytest.mark.parametrize("finish", ["late_lease", "runtime_fault", "unrelated_reject"])
def test_late_verifier_completion_preserves_winner_and_real_errors(
    tmp_path, monkeypatch, finish
):
    node = _recorder_task(
        "A", "implement recorder.py", [], ["pytest:tests/test_recorder.py"],
        3.0, ["recorder.py"], policy=["format_check", "rule_check", "code_test"],
    )
    provider = demo_dynamic_dag_provider(
        tasks=[node], per_attempt={"A": [_script(WRONG), _script(RECORDER_IMPL)]},
    )

    async def case():
        entered = {code: asyncio.Event() for code in (WRONG, RECORDER_IMPL)}
        release = {code: asyncio.Event() for code in (WRONG, RECORDER_IMPL)}
        code_runs = []
        lease_rejections = []
        injected = (
            RuntimeError("independent code verifier fault")
            if finish == "runtime_fault"
            else CommitRejected("independent code verifier rejection")
        )
        real_code_test = verifier_router.code_test

        async def gated_code_test(task, **kwargs):
            code = (kwargs["verification_copy"].root / "recorder.py").read_text()
            # Run the actual isolated pytest verifier, then hold its return to
            # the router. The loser reaches the real recorder only after accept.
            result = await real_code_test(task, **kwargs)
            code_runs.append((code, result.status))
            entered[code].set()
            await release[code].wait()
            if code == WRONG and finish != "late_lease":
                raise injected
            return result

        monkeypatch.setattr(verifier_router, "code_test", gated_code_test)
        config = OrchestratorConfig(
            evidence_root=tmp_path / "evidence",
            max_concurrency=2, candidates_per_task=2, verifier_workers=2,
            manager_after_failures=10, test_timeout_seconds=30,
        )
        async with Orchestrator(config, provider) as orch:
            mission = await orch.submit_mission(MissionSpec(
                goal=RECORDER_SPEC["goal"],
                success_criteria=("pytest:tests/test_recorder.py",),
                tenant_id="late-verifier", idempotency_key=finish,
                allowed_tools=tuple(RECORDER_SPEC["allowed_tools"]),
                budget=Budget(max_tokens=300_000, max_attempts=16),
                workspace_seed=RECORDER_SEED,
            ))
            real_hold = orch._hold_lease

            def observed_hold(attempt_id):
                try:
                    return real_hold(attempt_id)
                except CommitRejected as error:
                    lease_rejections.append((attempt_id, error))
                    raise

            monkeypatch.setattr(orch, "_hold_lease", observed_hold)

            async def reach_both_code_gates():
                await orch.recover()
                while not all(event.is_set() for event in entered.values()):
                    await orch._cycle()
                    await asyncio.sleep(0)

            try:
                await asyncio.wait_for(reach_both_code_gates(), 30)
                store = orch.store
                [task] = store.list_tasks(mission.id)
                attempts = store.list_attempts(task.id)
                assert len(attempts) == 2
                by_code = {
                    (orch.assembled.workspaces.root / a.id / "recorder.py").read_text(): a
                    for a in attempts
                }
                loser, winner = by_code[WRONG], by_code[RECORDER_IMPL]
                losing_result = store.find_result_for_attempt(loser.id)
                winning_result = store.find_result_for_attempt(winner.id)
                assert losing_result is not None and winning_result is not None
                loser_job = orch._verifying[losing_result.envelope.id]
                winner_job = orch._verifying[winning_result.envelope.id]
                assert set(code_runs) == {(WRONG, "FAIL"), (RECORDER_IMPL, "PASS")}

                # The real verifier invokes the public accept commit. No direct
                # table writes or fabricated accept receipt is involved.
                release[RECORDER_IMPL].set()
                assert await asyncio.wait_for(asyncio.shield(winner_job), 10) is True
                accepted_task = store.get_task(task.id)
                assert accepted_task.status is TaskStatus.COMPLETED
                assert accepted_task.accepted_result_id == winning_result.envelope.id
                assert store.get_attempt(winner.id).status is AttemptStatus.COMPLETED
                closed_loser = store.get_attempt(loser.id)
                assert closed_loser.status is AttemptStatus.SUPERSEDED
                assert closed_loser.failure["reason"] == "sibling_accepted"
                accepted = store.get_result(winning_result.envelope.id)
                assert (accepted.verification_state, accepted.verdict) == ("DONE", "PASS")
                assert store.get_mission(mission.id).status is MissionStatus.ACTIVE

                def budget_snapshot():
                    return (
                        orch.commit.ledger.account(mission_account(mission.id)).to_json(),
                        orch.commit.ledger.account(task_account(task.id)).to_json(),
                        tuple(orch.commit.ledger.reservation(a.id) for a in attempts),
                    )

                before_late = budget_snapshot()
                calls_before_late = provider.calls
                events_before_late = store.count_events(mission.id)
                release[WRONG].set()
                [outcome] = await asyncio.wait_for(
                    asyncio.gather(loser_job, return_exceptions=True), 10
                )
                assert budget_snapshot() == before_late
                assert provider.calls == calls_before_late == 9
                assert store.count_events(mission.id) == events_before_late

                if finish != "late_lease":
                    # Even with a legitimate accepted sibling, a fault from the
                    # code verifier itself is not the recorder's stale lease.
                    assert outcome is injected
                    assert lease_rejections == []
                    if finish == "runtime_fault":
                        with pytest.raises(RuntimeError, match="independent code verifier fault"):
                            await orch.run()
                    else:
                        with pytest.raises(
                            CommitRejected, match="independent code verifier rejection"
                        ):
                            orch._raise_if_verification_crashed()
                    assert store.get_mission(mission.id).status is MissionStatus.ACTIVE
                    assert winning_result.envelope.id in orch._verifying
                    assert orch._verifying[winning_result.envelope.id] is winner_job
                    assert losing_result.envelope.id not in orch._verifying
                    # No delayed callback may report the consumed error again.
                    await asyncio.sleep(0)
                    orch._raise_if_verification_crashed()
                    assert winner_job.result() is True
                    assert budget_snapshot() == before_late
                    return

                assert [aid for aid, _ in lease_rejections] == [loser.id]
                # Decisive RED on the base: outcome is the actual CommitRejected,
                # even if a later unconstrained run happened to finish Mission.
                assert outcome is True, outcome
                assert all(job.done() for job in orch._verifying.values())
                await asyncio.wait_for(orch.run(), 30)
                assert orch._verifying == {}
                assert store.get_mission(mission.id).status is MissionStatus.COMPLETED
                final_task = store.get_task(task.id)
                assert final_task.accepted_result_id == winning_result.envelope.id
                formal = [store.get_artifact(aid) for aid in final_task.accepted_artifacts]
                [recorder] = [a for a in formal if a.path == "recorder.py"]
                assert recorder.attempt_id == winner.id
                assert recorder.content_hash == _sha(RECORDER_IMPL)
                assert store.get_result(losing_result.envelope.id).verdict == "superseded"
                judged = (
                    orch.assembled.workspaces.root
                    / f"{mission.id}-judge-{orch.owner}-verify" / "recorder.py"
                )
                assert judged.read_text() == RECORDER_IMPL
                assert store.count_events(mission.id, "TaskCompleted") == 1
                assert store.count_events(mission.id, "VerificationPassed") == 1
                intents = store.list_intents(
                    "PENDING", "CLAIMED", "AGENT_CREATED", "SUBMITTED", "SETTLED", "FAILED"
                )
                assert len(intents) == 3 and all(i.state == "SETTLED" for i in intents)
                account = orch.commit.ledger.account(mission_account(mission.id))
                assert account.reserved_tokens == 0
                assert account.settled_tokens == provider.calls * 150 == 1350
                for attempt in attempts:
                    reservation = orch.commit.ledger.reservation(attempt.id)
                    assert reservation["state"] == "SETTLED"

                # Both completion tasks have been consumed. Driving again must
                # not verify, call the SDK, accept, or settle either one again.
                snapshot = budget_snapshot()
                count = store.count_events(mission.id)
                await asyncio.wait_for(orch.run(), 30)
                assert orch._verifying == {}
                assert budget_snapshot() == snapshot
                assert store.count_events(mission.id) == count
                assert provider.calls == 9
                assert len(code_runs) == 2
            finally:
                for event in release.values():
                    event.set()

    asyncio.run(case())
