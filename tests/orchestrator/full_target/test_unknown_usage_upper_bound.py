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


def test_a_resume_under_new_code_re_evaluates_the_closeout():
    """Desktop: the thermos Mission sat DRAINING under old code; after the upgrade
    nothing new happened on it, so the new counting rule never ran."""

    from agent_orchestrator.orchestrator.assurance_consumers import CLOSEOUT_SOURCE_EVENTS

    assert "PolicyInterpreterDrift" in CLOSEOUT_SOURCE_EVENTS


def _consumer(commit, store):
    consumer = AssuranceCloseoutConsumer.__new__(AssuranceCloseoutConsumer)
    consumer.commit, consumer.store = commit, store
    return consumer


def test_a_review_waiting_only_for_an_unanswerable_original_call_does_not_hold_the_closeout(tmp_path):
    """Real forced exit (2026-09-28): the backend was killed while a content review's model
    call was in flight.  After restart the review was recorded as waiting for original-call
    reconciliation (nothing reissued), the Task was redone and the Mission judged — but the
    SUBMITTED review intent kept the closeout DRAINING (21 minutes, until another restart
    let recovery fail it).  Such an intent no longer holds a judged Mission open when its
    charge is UNKNOWN; the charge is counted at the upper bound.  Goes through the same
    decision ``_evaluate_locked`` uses."""
    from agent_orchestrator.storage.store import DispatchIntent

    store, commit, mission = _world(tmp_path)
    ledger = commit._ledger

    def intent(key: str, state: str = "SUBMITTED", kind: str = "critic") -> str:
        store.insert_intent(DispatchIntent(
            intent_id=f"intent-{key}", kind=kind, subject_id=f"subject-{key}", mission_id=mission.id,
            state=state, version=1, creation_key=key, input_id="in", input_hash="a" * 64, config={},
            expected_turn_id="turn", agent_id="agent", receipt=None, lease_owner=None, lease_expires_at=None,
            replays=0, created_at=store.now))
        return f"intent-{key}"

    def waiting(intent_id: str) -> None:
        store.insert_receipt(commit_id="assurance-provider-wait:" + intent_id, kind="AssuranceProviderReconciliationRequired",
                             subject_id=intent_id, base_version=0, proposal_hash="0" * 64, receipt={"intent_id": intent_id})

    def unknown_charge(key: str) -> None:
        with store.transaction():
            ledger.reserve(account_id=f"budget:{mission.id}", subject_id=f"subject-{key}", tokens=5_000, cost_micros=0, counts_attempt=False)
            ledger.import_usage(subject_id=f"subject-{key}", mission_id=mission.id, facts=[UsageFact(f"call-{key}", 0, 0, 0, unknown=True)])

    consumer = _consumer(commit, store)

    def decide(intents):
        reserved = [r[0] for r in store.connection.execute(
            "SELECT subject_id FROM budget_reservations WHERE mission_id=? AND state='RESERVED' ORDER BY subject_id", (mission.id,))]
        return consumer._drain_decision(mission.id, reasons=[], unknown_effects=[], open_intents=intents,
                                        open_reservations=reserved, usage_fully_known=ledger.usage_flags(mission.id)["usage_fully_known"])

    cut = intent("cut")
    waiting(cut)
    unknown_charge("cut")
    ready = decide([cut])
    assert ready["state"] == "READY" and ready["usage_counted_at_upper_bound"]["subjects"] == ["subject-cut"]

    # a second, ordinary open intent still holds the closeout
    plain = intent("plain")
    unknown_charge("plain")
    held = decide([cut, plain])
    assert held["state"] == "DRAINING" and "OPEN_INTENTS" in held["reasons"]

    # a worker intent with the same record is never exempt; neither is a claimed one
    worker, claimed, known = intent("worker", kind="attempt"), intent("claimed", state="CLAIMED"), intent("known")
    for i in (worker, claimed, known):
        waiting(i)
    unknown_charge("worker")
    unknown_charge("claimed")
    with store.transaction():  # a recorded wait whose charge is known is not exempt
        ledger.reserve(account_id=f"budget:{mission.id}", subject_id="subject-known", tokens=5_000, cost_micros=0, counts_attempt=False)
    assert consumer._awaiting_original_reconciliation([cut, plain, worker, claimed, known]) == {cut}
    store.close()


def test_an_exempt_review_whose_reservation_is_gone_still_drains_on_unknown_usage(tmp_path):
    """The exemption only lets the upper-bound rule count the charge; if the unknown charge
    is not held by an open reservation, the closeout keeps draining on the unknown usage."""
    from agent_orchestrator.storage.store import DispatchIntent

    store, commit, mission = _world(tmp_path)
    store.insert_intent(DispatchIntent(
        intent_id="intent-cut", kind="critic", subject_id="subject-cut", mission_id=mission.id, state="SUBMITTED",
        version=1, creation_key="k", input_id="in", input_hash="a" * 64, config={}, expected_turn_id="turn",
        agent_id="agent", receipt=None, lease_owner=None, lease_expires_at=None, replays=0, created_at=store.now))
    store.insert_receipt(commit_id="assurance-provider-wait:x", kind="AssuranceProviderReconciliationRequired",
                         subject_id="intent-cut", base_version=0, proposal_hash="0" * 64, receipt={})
    with store.transaction():
        commit._ledger.import_usage(subject_id="subject-cut", mission_id=mission.id,
                                    facts=[UsageFact("call-cut", 0, 0, 0, unknown=True)])
    out = _consumer(commit, store)._drain_decision(
        mission.id, reasons=[], unknown_effects=[], open_intents=["intent-cut"], open_reservations=[],
        usage_fully_known=commit._ledger.usage_flags(mission.id)["usage_fully_known"])
    assert out["state"] == "DRAINING" and out["reasons"] == ["USAGE_UNKNOWN"]
    store.close()
