# SPDX-License-Identifier: Apache-2.0
"""C08: late / repeated accounting is separate from review outcomes and lifecycle.

Assured fixture runtime (real Store/Commit/ledger, actual AgentRuntime with a
scripted reviewer). The original review call's cost is imported once through
the production collector; the fixture Worker's cost is imported as UNKNOWN. The
offline recovery scan (``import_late_accounting``) and repeated / late usage
imports are then driven by hand, before and after the Mission stops, and again
from a second connection to the same database. No real model.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from agent_orchestrator.contracts.state_machines import MissionStatus, TaskStatus
from agent_orchestrator.governance.budgets import UsageFact
from agent_orchestrator.orchestrator.accounting_recovery import import_late_accounting
from agent_orchestrator.storage.store import Store

SDK_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(SDK_ROOT / "scripts/assurance_seams"))

ACCEPT_REPLY = {"schema_version": 2, "verdict": "ACCEPT", "assessments": [
    {"criterion_id": "criterion-report", "verdict": "PASS", "evidence_ids": [], "reason": "fixture", "limitations": []}],
    "findings": []}


def _usage_rows(store, mission_id):
    return [tuple(r) for r in store.connection.execute(
        "SELECT usage_ref, subject_id, input_tokens, output_tokens, cost_micros, unpriced, unknown "
        "FROM imported_usage WHERE mission_id=? ORDER BY usage_ref", (mission_id,))]


def _reservations(store):
    return [tuple(r) for r in store.connection.execute(
        "SELECT * FROM budget_reservations ORDER BY subject_id")]


def _lifecycle(store, mission_id):
    return {
        "mission": store.get_mission(mission_id).status,
        "tasks": sorted((t.id, str(t.status)) for t in store.list_tasks(mission_id)),
        "attempts": store.connection.execute("SELECT COUNT(*) FROM attempts WHERE mission_id=?", (mission_id,)).fetchone()[0],
        "intents": sorted(tuple(r) for r in store.connection.execute(
            "SELECT intent_id, state FROM dispatch_intents WHERE mission_id=?", (mission_id,))),
    }


def test_late_accounting_is_separate(tmp_path):
    from _assured_fixture import AssuredRuntime

    async def body():
        async with AssuredRuntime(tmp_path, [ACCEPT_REPLY]) as rt:
            store, commit, mission_id = rt.store, rt.commit, rt.mission.id
            ledger = commit.ledger
            verdict, record = await rt.run_critic()
            assert verdict.passed and rt.provider.calls == 1
            rt.settle_fixture_worker()  # the Worker's price is unknown: an explicit hold
            worker_subject = rt.stored.envelope.attempt_id
            review_subjects = [r[0] for r in store.connection.execute(
                "SELECT subject_id FROM dispatch_intents WHERE mission_id=? AND subject_id LIKE '%assurance%'",
                (mission_id,))]
            usage_before = _usage_rows(store, mission_id)
            reservations_before = _reservations(store)
            lifecycle_before = _lifecycle(store, mission_id)
            assert ledger.has_unknown_usage(worker_subject)
            review_tokens = sum(ledger.known_usage_for(subject)[0] for subject in review_subjects)
            assert review_tokens == 20  # the one real review call, priced once
            # Repeated / late imports of the same facts change nothing.
            assert commit.import_usage(worker_subject, mission_id,
                                       (UsageFact("fixture-worker-usage", 10, 10, None, unknown=True),)) == 0
            for subject in review_subjects:
                refs = [r[0] for r in store.connection.execute(
                    "SELECT usage_ref FROM imported_usage WHERE subject_id=?", (subject,))]
                assert commit.import_usage(subject, mission_id, tuple(
                    UsageFact(ref, 10, 10, 0) for ref in refs)) == 0
            assert _usage_rows(store, mission_id) == usage_before
            # The offline recovery scan: nothing to release, nothing revived, no raise.
            orch = rt.orch
            orch.assembled.pools = {}
            for _ in range(3):
                import_late_accounting(orch)
                assert _usage_rows(store, mission_id) == usage_before
                assert _reservations(store) == reservations_before
                assert _lifecycle(store, mission_id) == lifecycle_before
                assert ledger.has_unknown_usage(worker_subject)  # the hold is retained
            assert rt.provider.calls == 1
            # The Mission stops: late accounting still cannot revive work or authority.
            commit.cancel_mission(mission_id)
            assert store.get_mission(mission_id).status is MissionStatus.CANCELLED
            stopped = _lifecycle(store, mission_id)
            assert all(status in {str(TaskStatus.CANCELLED), str(TaskStatus.COMPLETED), str(TaskStatus.FAILED)}
                       for _, status in stopped["tasks"])
            for _ in range(2):
                import_late_accounting(orch)
                assert commit.import_usage(worker_subject, mission_id,
                                           (UsageFact("fixture-worker-usage", 10, 10, None, unknown=True),)) == 0
                assert _lifecycle(store, mission_id) == stopped
                assert _usage_rows(store, mission_id) == usage_before
                assert ledger.has_unknown_usage(worker_subject)
            assert rt.provider.calls == 1
            # A later known price for the same unknown fact lands once and settles nothing by itself.
            assert commit.import_usage(worker_subject, mission_id,
                                       (UsageFact("fixture-worker-usage", 10, 10, 7, unknown=False),)) == 1
            assert commit.import_usage(worker_subject, mission_id,
                                       (UsageFact("fixture-worker-usage", 10, 10, 7, unknown=False),)) == 0
            assert ledger.known_usage_for(worker_subject) == (20, 7, False)
            assert _lifecycle(store, mission_id) == stopped
            # Replay after a "hard exit": a fresh connection sees exactly the same facts.
            other = Store.open(store.path)
            try:
                assert _usage_rows(other, mission_id) == _usage_rows(store, mission_id)
                assert _reservations(other) == _reservations(store)
            finally:
                other.close()

    asyncio.run(body())
