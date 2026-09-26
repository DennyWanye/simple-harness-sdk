# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""User decision 2026-09-26: an UNKNOWN charge is counted at its upper bound at closeout.

Desktop run (thermos Mission): every criterion was met and the root review accepted,
but one failed attempt's UNKNOWN charge kept its 57k reservation held forever, so the
closeout stayed DRAINING and the Mission never ended.  The charge is now counted at
the larger of the reservation and the known facts — never less — and the Mission can
close; the usage fact itself stays unknown (the truth).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_orchestrator.contracts import Budget
from agent_orchestrator.governance.budgets import UsageFact
from agent_orchestrator.orchestrator.assurance_consumers import AssuranceCloseoutConsumer
from agent_orchestrator.orchestrator.commit_service import CommitService, MissionSpec
from agent_orchestrator.storage.store import Store


def _world(tmp_path):
    store = Store.open(tmp_path / "orchestrator.db")
    commit = CommitService(store, global_budget=Budget(max_tokens=1_000_000, max_attempts=20))
    mission, _ = commit.create_mission(MissionSpec(
        goal="g", success_criteria=("file:a.md",), tenant_id="t", idempotency_key="k",
        budget=Budget(max_tokens=200_000, max_attempts=6)))
    return store, commit, mission


def test_a_held_unknown_charge_is_counted_at_its_upper_bound(tmp_path):
    store, commit, mission = _world(tmp_path)
    ledger = commit._ledger
    account = f"budget:{mission.id}"
    with store.transaction():
        ledger.reserve(account_id=account, subject_id="s-1", tokens=57_000, cost_micros=0, counts_attempt=False)
        ledger.import_usage(subject_id="s-1", mission_id=mission.id, facts=[
            UsageFact("call-1", 20_000, 1_000, 0), UsageFact("call-2", 0, 0, 0, unknown=True)])
    assert ledger.has_unknown_usage("s-1")
    before = ledger.account(account)
    with store.transaction():
        settled = ledger.settle_at_upper_bound(subject_id="s-1")
    after = ledger.account(account)
    assert settled["state"] == "SETTLED" and settled["settled_tokens"] == 57_000  # max(reserved, known)
    assert after.reserved_tokens == before.reserved_tokens - 57_000
    assert after.settled_tokens == before.settled_tokens + 57_000
    assert ledger.usage_flags(mission.id)["budget_conserved"] is True
    assert ledger.usage_flags(mission.id)["usage_fully_known"] is False  # the fact stays unknown
    store.close()


def test_known_facts_above_the_reservation_are_never_cut(tmp_path):
    store, commit, mission = _world(tmp_path)
    ledger = commit._ledger
    with store.transaction():
        ledger.reserve(account_id=f"budget:{mission.id}", subject_id="s-2", tokens=10_000, cost_micros=0, counts_attempt=False)
        ledger.import_usage(subject_id="s-2", mission_id=mission.id, facts=[
            UsageFact("call-1", 30_000, 2_000, 0), UsageFact("call-2", 0, 0, 0, unknown=True)])
    with store.transaction():
        assert ledger.settle_at_upper_bound(subject_id="s-2")["settled_tokens"] == 32_000
    store.close()


def test_the_closeout_counts_only_when_every_unknown_charge_is_covered(tmp_path):
    store, commit, mission = _world(tmp_path)
    ledger = commit._ledger
    with store.transaction():
        ledger.reserve(account_id=f"budget:{mission.id}", subject_id="s-1", tokens=5_000, cost_micros=0, counts_attempt=False)
        ledger.import_usage(subject_id="s-1", mission_id=mission.id,
                            facts=[UsageFact("call-1", 0, 0, 0, unknown=True)])
        ledger.reserve(account_id=f"budget:{mission.id}", subject_id="s-2", tokens=5_000, cost_micros=0, counts_attempt=False)
    fake = SimpleNamespace(commit=commit, store=store)
    plan = AssuranceCloseoutConsumer._upper_bound_plan
    assert plan(fake, mission.id, ["s-1"]) == {"subjects": ["s-1"], "already_counted": []}
    # A reservation that is not held by an unknown charge is ordinary draining.
    assert plan(fake, mission.id, ["s-1", "s-2"]) is None
    # An unknown charge whose reservation is not in the plan blocks it.
    assert plan(fake, mission.id, []) is None
    store.close()
