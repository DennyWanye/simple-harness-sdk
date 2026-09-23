# SPDX-License-Identifier: Apache-2.0
"""Stateful (seeded random operation sequences) over the assured review/acceptance path.

No hypothesis in this venv (the plan forbids installing runtime deps for
acceptance), so the sequences come from ``random.Random(seed)``. Each step is
one production entry (durable tick, critic entry, router layer, usage import,
acceptance writer, fixed-caller snapshot) applied in a random order, including
out-of-order and repeated calls; after every step the invariants below are
re-read from the Store. Six seeds × 14 steps.
"""

from __future__ import annotations

import asyncio
import random
import sys
from pathlib import Path

import pytest

from agent_orchestrator.contracts.state_machines import MissionStatus
from agent_orchestrator.governance.budgets import BudgetError
from agent_orchestrator.orchestrator.resolution_commits import ResolutionCommitRejected

SDK_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(SDK_ROOT / "scripts/assurance_seams"))

ACCEPT_REPLY = {"schema_version": 2, "verdict": "ACCEPT", "assessments": [
    {"criterion_id": "criterion-report", "verdict": "PASS", "evidence_ids": [], "reason": "fixture", "limitations": []}],
    "findings": []}
OPS = ("tick", "critic", "layer", "usage", "accept", "snapshot")


def _count(store, sql, *params):
    return store.connection.execute(sql, params).fetchone()[0]


def _epoch(store, mission_id):
    row = store.connection.execute(
        "SELECT epoch FROM validity_epochs WHERE mission_id=? AND scope_id=?", (mission_id, "assurance:mission")).fetchone()
    return 0 if row is None else int(row[0])


class Model:
    """What the sequence has legitimately established so far."""

    def __init__(self):
        self.record = None
        self.layer = False
        self.usage = False
        self.accepted = False


async def _sequence(tmp_path, seed):
    from _assured_fixture import AssuredRuntime

    rng = random.Random(seed)
    async with AssuredRuntime(tmp_path, [ACCEPT_REPLY]) as rt:
        store, mission_id = rt.store, rt.mission.id
        model = Model()
        last_epoch = _epoch(store, mission_id)
        trace = []
        for step in range(14):
            op = rng.choice(OPS)
            trace.append(op)
            before = {
                "acceptances": _count(store, "SELECT COUNT(*) FROM acceptances WHERE mission_id=?", mission_id),
                "certificates": _count(store, "SELECT COUNT(*) FROM assurance_use_certificates WHERE mission_id=?", mission_id),
                "records": _count(store, "SELECT COUNT(*) FROM review_records WHERE mission_id=? AND official=1", mission_id),
                "invocations": _count(store, "SELECT COUNT(*) FROM assurance_review_invocations WHERE mission_id=?", mission_id),
                "attempts": _count(store, "SELECT COUNT(*) FROM attempts WHERE mission_id=?", mission_id),
            }
            if op == "tick":
                for _ in range(rng.randint(1, 3)):
                    await rt.pump.tick()
            elif op == "critic":
                verdict, record = await rt.run_critic()
                assert verdict.passed
                if model.record is not None:
                    assert record.record_id == model.record.record_id  # replay, same official record
                model.record = record
            elif op == "layer":
                if model.record is None:
                    continue
                rt.record_critic_layer(model.record)
                model.layer = True
            elif op == "usage":
                rt.settle_fixture_worker()  # first time imports, later times dedupe
                model.usage = True
            elif op == "accept":
                # The writer needs the official record and a closed original executor
                # (usage imported); the router's layer annotation is not a licence.
                if model.record is not None and model.usage:
                    completed = rt.accept_now()
                    assert completed.accepted_result_id == rt.stored.envelope.id
                    model.accepted = True
                elif model.record is None:
                    with pytest.raises(ResolutionCommitRejected) as refused:
                        rt.accept_now()
                    assert refused.value.reason == "REVIEW_NOT_OFFICIAL", trace
                else:
                    with pytest.raises(BudgetError):
                        rt.accept_now()
            elif op == "snapshot":
                from agent_orchestrator.api.assurance import AssuranceApi  # fixed caller, read only
                from agent_orchestrator.governance.permissions import Principal
                api = AssuranceApi(rt.commit, tenant_id=rt.mission.tenant_id, principal=Principal("exec-current-user"),
                                   validity=rt.validity, host_fingerprint="ab" * 32)
                try:
                    api.snapshot({"schema_version": 1, "request_id": f"r-{seed}-{step}", "mission_id": mission_id})
                except Exception:  # noqa: BLE001 - a read never changes state; refusal shape is C07's job
                    pass
            after = {
                "acceptances": _count(store, "SELECT COUNT(*) FROM acceptances WHERE mission_id=?", mission_id),
                "certificates": _count(store, "SELECT COUNT(*) FROM assurance_use_certificates WHERE mission_id=?", mission_id),
                "records": _count(store, "SELECT COUNT(*) FROM review_records WHERE mission_id=? AND official=1", mission_id),
                "invocations": _count(store, "SELECT COUNT(*) FROM assurance_review_invocations WHERE mission_id=?", mission_id),
                "attempts": _count(store, "SELECT COUNT(*) FROM attempts WHERE mission_id=?", mission_id),
            }
            # Invariants, re-read from the Store after every step.
            assert not store.connection.in_transaction, trace
            assert rt.provider.calls == (1 if model.record is not None else 0), trace
            assert after["records"] == (1 if model.record is not None else 0), trace
            assert after["invocations"] == (1 if model.record is not None else 0), trace
            assert after["acceptances"] == (1 if model.accepted else 0), trace
            assert after["certificates"] <= 1 and after["certificates"] == (1 if model.accepted else 0), trace
            assert after["attempts"] == before["attempts"], trace  # no worker ever reopened
            if op in ("tick", "snapshot"):
                assert after == before, trace  # reads and idle ticks write no acceptance/record rows
            epoch = _epoch(store, mission_id)
            assert epoch >= last_epoch, trace
            last_epoch = epoch
            assert store.get_mission(mission_id).status is MissionStatus.ACTIVE, trace
            assert not rt.pump.rejections, trace
        return trace, model


@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5, 6])
def test_random_sequences_keep_invariants(tmp_path, seed):
    trace, model = asyncio.run(_sequence(tmp_path, seed))
    assert len(trace) == 14
