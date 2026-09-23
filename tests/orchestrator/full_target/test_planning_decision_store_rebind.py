# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""H1-H: the constrained, transactional rebind of a request's answering intent.

Addendum §5.2 asks the §34 binding to name the intent that *actually* answered a
request.  The opener and its one format retry are the same request, so they share
one row and one identity — and the retry is the only case where the answering
intent differs from the opener.  ``insert_planning_request`` cannot express that
(identical content is idempotent, different content is a conflict), so the store
grows one explicit, constrained operation:

* it never inserts: a rebind of a request that has no row is refused;
* it never touches the frozen identity: ``created_at`` keeps the moment the request
  was established, and no column except ``intent_id`` is written;
* it is idempotent: re-applying the move already made writes nothing;
* it is validated before the statement runs, so a refusal leaves the row byte for
  byte as it was;
* it runs inside the caller's transaction (``Store.transaction()``) and never opens
  a connection of its own.

The signature is ``(request_id, expected_opener_intent_id, retry_intent_id)``: three
strings, because the store writes exactly one column.  ``expected_opener_intent_id``
is the opener the caller read from the row it is re-answering — the store refuses the
move unless the row still names exactly that intent, so a rebind can never silently
overwrite a binding that has already moved on (``§34``).  A caller therefore reads the
row, then asserts what it read, and re-reading a row it already moved is the idempotent
replay.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

import pytest

from agent_orchestrator.contracts import Budget, Mission, MissionStatus
from agent_orchestrator.storage.planning_decision_store import PlanningDecisionStore
from agent_orchestrator.storage.store import InjectedCrash, Store, StoreConflict

# The opener row and the decision recorder of the H1-B suite are reused verbatim: this
# file pins what the *rebind* does beside them, so a second copy of the same builders
# would be a second definition of the row being re-answered.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_planning_decision_store import (  # noqa: E402
    HASH_A,  # noqa: F401 - re-exported for readers of this module
    HASH_B,
    MISSION,
    record,
    request_binding,
)

OPENER = "intent-1"
RETRY = "intent-retry-2"

# --------------------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------------------


def _mission(mission_id: str = MISSION) -> Mission:
    return Mission(
        id=mission_id,
        goal="g",
        success_criteria=("ok",),
        stop_conditions=(),
        allowed_tools=(),
        risk_level="sandbox",
        budget=Budget(max_tokens=1000, max_attempts=2),
        tenant_id="t",
        status=MissionStatus.CREATED,
        created_at=1.0,
        version=1,
        idempotency_key=mission_id,
    )


@pytest.fixture
def store(tmp_path) -> Store:
    opened = Store.open(tmp_path / "orchestrator.db")
    opened.insert_mission(_mission(), spec_hash="h")
    return opened


@pytest.fixture
def decisions(store: Store) -> PlanningDecisionStore:
    return PlanningDecisionStore(store)


def _row_bytes(store: Store, request_id: str) -> tuple[Any, ...]:
    """The whole stored row, verbatim, so "unchanged" means byte for byte."""

    row = store.connection.execute(
        "SELECT rowid,* FROM planning_requests WHERE request_id = ?", (request_id,)
    ).fetchone()
    assert row is not None
    return tuple(row)


def _rebind(decisions: PlanningDecisionStore, request_id: str, *intents: str) -> Any:
    """The store's own call shape: the request, then the opener and its answering intent."""

    return decisions.rebind_planning_request_intent(request_id, *intents)


def _bound(decisions: PlanningDecisionStore) -> Any:
    """A stored opener row: the request a retry will re-answer."""

    opener = request_binding()
    decisions.insert_planning_request(opener)
    assert decisions.get_planning_request(opener.request_id) is not None
    assert opener.intent_id == OPENER
    return opener


# --------------------------------------------------------------------------------------
# R1: the rebind moves exactly one column, and only after validation
# --------------------------------------------------------------------------------------


def test_the_rebind_moves_the_answering_intent_and_keeps_the_request_identity(
    store: Store, decisions: PlanningDecisionStore
) -> None:
    """R1: same request, new answering intent — the one thing the retry changes."""

    opener = _bound(decisions)

    rebound = _rebind(decisions, opener.request_id, OPENER, RETRY)

    assert rebound.intent_id == RETRY
    assert rebound.request_id == opener.request_id
    # The request was established when the opener was bound; asking again does not move
    # that moment, and no other §34 field moves either.
    document = rebound.to_json()
    assert document["created_at"] == opener.created_at
    assert {key: value for key, value in document.items() if key != "intent_id"} == {
        key: value for key, value in opener.to_json().items() if key != "intent_id"
    }
    stored = decisions.get_planning_request(opener.request_id)
    assert stored == rebound
    assert stored.intent_id == RETRY


def test_the_rebind_writes_only_the_intent_column(
    store: Store, decisions: PlanningDecisionStore
) -> None:
    """R1b: the statement is one column wide — nothing else in the row is re-spelled."""

    opener = _bound(decisions)
    before = _row_bytes(store, opener.request_id)

    _rebind(decisions, opener.request_id, OPENER, RETRY)

    after = _row_bytes(store, opener.request_id)
    # The columns are read in the schema's own order, so the comparison describes the
    # statement rather than the caller's dictionary order.
    from agent_orchestrator.storage.planning_decision_store import _REQUEST_FIELDS

    differing = [
        column
        for column, was, now in zip(("rowid", *_REQUEST_FIELDS), before, after, strict=True)
        if was != now
    ]
    assert differing == ["intent_id"]


def test_the_rebind_is_idempotent_and_writes_nothing_the_second_time(
    store: Store, decisions: PlanningDecisionStore
) -> None:
    """R2: the same answering intent twice is one write, not two.

    The caller re-reads the row it moved, so the second call names the retry as the
    opener: that is the replay, and it must return the same binding and touch nothing.
    """

    opener = _bound(decisions)
    first = _rebind(decisions, opener.request_id, OPENER, RETRY)
    before = _row_bytes(store, opener.request_id)

    replay = _rebind(decisions, opener.request_id, RETRY, RETRY)

    assert replay == first
    assert _row_bytes(store, opener.request_id) == before
    assert replay.created_at == opener.created_at


def test_the_replay_returns_before_the_ladder_gate_refuses_the_spent_retry(
    store: Store, decisions: PlanningDecisionStore
) -> None:
    """R2b: a retry that already landed replays itself, attempt 1 notwithstanding.

    This is the order the two halves of the rebind have to run in.  Once the re-ask has
    landed, the request's attempt 1 exists and §36 closes the ladder there — which is
    exactly what ``_require_reopenable`` refuses, and must refuse, for a *third* intent.
    But a completed retry re-reading its own row is not asking to re-bind anything: the
    move it names is already the stored one, so it writes nothing and needs no permission
    from the ladder.  Gating it behind the reopen check makes the retry's own
    re-application fail on the very state its success produces.
    """

    from agent_orchestrator.contracts.planning_decisions import PlanningDecisionStatus

    opener = _bound(decisions)
    first = _rebind(decisions, opener.request_id, OPENER, RETRY)
    # The re-ask lands: attempt 1, the last ordinal §5.2 allows, is now recorded.
    record(
        decisions,
        attempt_ordinal=1,
        raw_output_hash=HASH_B,
        status=PlanningDecisionStatus.UNREADABLE,
        detail={"stage": "decode"},
    )
    before = _row_bytes(store, opener.request_id)

    replay = _rebind(decisions, opener.request_id, RETRY, RETRY)

    assert replay == first
    assert replay.intent_id == RETRY
    assert _row_bytes(store, opener.request_id) == before
    assert replay.created_at == opener.created_at

    # ...and the gate is still load-bearing beside it: a third intent is refused on the
    # spent ladder, and the refusal leaves the row alone.
    with pytest.raises(StoreConflict):
        _rebind(decisions, opener.request_id, RETRY, "intent-retry-3")

    assert _row_bytes(store, opener.request_id) == before
    assert decisions.get_planning_request(opener.request_id).intent_id == RETRY


def test_a_rebind_of_an_unrecorded_request_is_refused_and_inserts_nothing(
    store: Store, decisions: PlanningDecisionStore
) -> None:
    """R3: a rebind is not an insert; there is no request to re-answer."""

    with pytest.raises(StoreConflict):
        _rebind(decisions, "never-inserted", OPENER, RETRY)

    count = store.connection.execute("SELECT count(*) FROM planning_requests").fetchone()[0]
    assert count == 0


def test_a_rebind_that_names_the_wrong_opener_is_a_conflict(
    store: Store, decisions: PlanningDecisionStore
) -> None:
    """R4: two intents cannot both claim one request — the opener must be the stored one."""

    opener = _bound(decisions)
    before = _row_bytes(store, opener.request_id)

    with pytest.raises(StoreConflict):
        _rebind(decisions, opener.request_id, "intent-someone-else", RETRY)

    # A refusal leaves the row byte for byte as it was.
    assert _row_bytes(store, opener.request_id) == before
    assert decisions.get_planning_request(opener.request_id).intent_id == OPENER


def test_a_third_intent_cannot_claim_a_request_that_already_moved(
    store: Store, decisions: PlanningDecisionStore
) -> None:
    """R5: §5.2 allows exactly one re-ask, so the identity has one successor.

    After the retry has answered, the row names the retry.  A later intent that still
    asserts the *opener* it read is refused — that is the third intent trying to reuse
    one request's identity, and the refusal is what makes the bound durable rather than
    a property of the ladder that happens to call it.
    """

    opener = _bound(decisions)
    _rebind(decisions, opener.request_id, OPENER, RETRY)
    before = _row_bytes(store, opener.request_id)

    with pytest.raises(StoreConflict):
        _rebind(decisions, opener.request_id, OPENER, "intent-retry-3")

    assert _row_bytes(store, opener.request_id) == before
    assert decisions.get_planning_request(opener.request_id).intent_id == RETRY


def test_a_rebind_joins_the_callers_transaction(
    store: Store, decisions: PlanningDecisionStore
) -> None:
    """R6: the rebind and the decision beside it land together, or not at all.

    The store composes an open ``Store.transaction()`` and never opens its own, so a
    caller keeps the retry's rebind and its attempt-1 decision atomic.
    """

    from agent_orchestrator.contracts.planning_decisions import PlanningDecisionStatus

    opener = _bound(decisions)

    store.arm("h1h-rebind")
    with pytest.raises(InjectedCrash):
        with store.transaction():
            _rebind(decisions, opener.request_id, OPENER, RETRY)
            record(
                decisions,
                attempt_ordinal=1,
                raw_output_hash=HASH_B,
                status=PlanningDecisionStatus.UNREADABLE,
                detail={"stage": "decode"},
            )
            store.fault("h1h-rebind")

    assert decisions.get_planning_request(opener.request_id).intent_id == OPENER
    assert decisions.get_planning_decision_by_attempt(opener.request_id, 1) is None

    # ...and the same two writes inside one committed transaction both land.
    with store.transaction():
        _rebind(decisions, opener.request_id, OPENER, RETRY)
        record(
            decisions,
            attempt_ordinal=1,
            raw_output_hash=HASH_B,
            status=PlanningDecisionStatus.UNREADABLE,
            detail={"stage": "decode"},
        )
    assert decisions.get_planning_request(opener.request_id).intent_id == RETRY
    assert decisions.get_planning_decision_by_attempt(opener.request_id, 1) is not None


def test_the_rebind_refuses_a_row_that_is_not_readable(
    store: Store, decisions: PlanningDecisionStore
) -> None:
    """R7: validation reads the stored row through the binding contract.

    A damaged row cannot be re-answered: the content comparison is the contract's, and a
    refusal must leave the row alone rather than derive a half-update.
    """

    opener = _bound(decisions)
    with store.transaction() as connection:
        connection.execute(
            "UPDATE planning_requests SET package_hash = ? WHERE request_id = ?",
            ("not-a-hash", opener.request_id),
        )
    before = _row_bytes(store, opener.request_id)

    with pytest.raises(Exception) as raised:  # noqa: B017 - the codec's own refusal type
        _rebind(decisions, opener.request_id, OPENER, RETRY)

    assert not isinstance(raised.value, sqlite3.IntegrityError)
    assert _row_bytes(store, opener.request_id) == before


def test_the_insert_path_is_unchanged_by_the_rebind(
    store: Store, decisions: PlanningDecisionStore
) -> None:
    """R8: the old entry point keeps its own semantics, byte for byte.

    Same content is still idempotent, a different intent is still a conflict because the
    insert path has no way to narrow a difference to the one column a retry may move, and
    a first insert still lands the row the caller handed over.
    """

    binding = request_binding()
    first = decisions.insert_planning_request(binding)
    assert first == binding
    assert decisions.insert_planning_request(request_binding()) == first
    # A different *intent* is refused here too: only the rebind may move that column.
    with pytest.raises(StoreConflict):
        decisions.insert_planning_request(
            type(binding)(**{**binding.to_json(), "intent_id": RETRY})
        )
    with pytest.raises(StoreConflict):
        decisions.insert_planning_request(request_binding(base_plan_revision=4))
    assert decisions.get_planning_request(binding.request_id) == first


def test_the_rebound_row_still_round_trips_through_the_public_reader(
    store: Store, decisions: PlanningDecisionStore
) -> None:
    """R9: the reader is the proof, not the writer's return value."""

    opener = _bound(decisions)
    _rebind(decisions, opener.request_id, OPENER, RETRY)

    stored = decisions.get_planning_request(opener.request_id)
    assert stored is not None
    document = stored.to_json()
    assert document["intent_id"] == RETRY
    assert document["created_at"] == opener.created_at
    assert {
        key: value for key, value in document.items() if key not in {"intent_id", "created_at"}
    } == {
        key: value
        for key, value in opener.to_json().items()
        if key not in {"intent_id", "created_at"}
    }
    # The stored row itself carries the retry's intent id, not a shadow copy.
    row = store.connection.execute(
        "SELECT intent_id, created_at FROM planning_requests WHERE request_id = ?",
        (opener.request_id,),
    ).fetchone()
    assert (row["intent_id"], row["created_at"]) == (RETRY, opener.created_at)
    assert json.loads(json.dumps(document))["intent_id"] == RETRY


def test_the_rebind_validates_its_identifiers_before_writing(
    store: Store, decisions: PlanningDecisionStore
) -> None:
    """R10: the three arguments are validated, so a bad id cannot reach the statement."""

    from agent_orchestrator.contracts.models import ContractError

    opener = _bound(decisions)
    before = _row_bytes(store, opener.request_id)

    for arguments in (
        ("", OPENER, RETRY),
        (opener.request_id, "", RETRY),
        (opener.request_id, OPENER, ""),
        (opener.request_id, OPENER, 7),
    ):
        with pytest.raises(ContractError):
            decisions.rebind_planning_request_intent(*arguments)

    assert _row_bytes(store, opener.request_id) == before
