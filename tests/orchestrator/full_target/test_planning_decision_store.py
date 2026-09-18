# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""H1-B red tests: migration 19 and the planning-decision store (V2 §8.2, §35, §36).

Three things are pinned here:

1. **Migration 19 is additive.**  It is one migration that creates three tables,
   every one of them STRICT; migrations 1–18 keep their bytes and their checksums,
   and a library deployed at 18 upgrades in place with every old row intact.
2. **Identity is enforced by the schema and by the store.**  ``(request_id,
   attempt_ordinal)`` is unique in SQLite, replaying the same raw output returns the
   same row instead of a second one, and the same ordinal with different bytes is an
   identity conflict — never a silent UPDATE.
3. **The library decides the mode.**  A legacy mission has no
   ``mission_planning_protocols`` row and never writes any of the three tables.

Definitions live in ``test_planning_decision_request_binding.py``; this file is the
storage side: heads, DDL, idempotency, conflicts, foreign keys and crash recovery.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from agent_orchestrator.contracts import Budget, Mission, MissionStatus
from agent_orchestrator.contracts.models import ContractError
from agent_orchestrator.contracts.planning_decisions import (
    PlanningDecisionStatus,
    PlanningRequestBinding,
    compute_decision_id,
)
from agent_orchestrator.storage import planning_decision_schema, schema
from agent_orchestrator.storage.planning_decision_store import PlanningDecisionStore
from agent_orchestrator.storage.store import InjectedCrash, Store, StoreConflict

MISSION = "mission-1"
PROTOCOL = "planning-decision-v1"
PROMPT = "planner-hierarchical-v8"
PACKAGE_VERSION = 4

HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
HASH_D = "d" * 64
HASH_E = "e" * 64

STEP02_TESTS = Path(__file__).resolve().parents[1] / "step02" / "test_store_and_budgets.py"

#: Migrations 16, 17 and 18 as shipped.  Repeated here as literals, independently of
#: ``test_htn_store.py``: a schema head may move forward, the old bytes may not.
FROZEN_16_CHECKSUM = "239e8fdcd6a3f4ebb6fbe0073d416c2f6607ca927dc2a324bcdd322fcf369f62"
FROZEN_17_CHECKSUM = "ffb48ba4314a625adcba69e33e83bd8b06f921621282e16beb62c48da66531b1"
FROZEN_18_CHECKSUM = "a24b4ef345f3ef46ec4b43ee3d68da5f6cc3372aae7968dcc0b7b7a9efd671d8"

#: Migration 19 as shipped.  Same discipline as 16/17/18: a byte in this DDL is frozen,
#: and a quiet edit has to show up as a checksum failure here, not as a support ticket
#: from a library whose upgrade suddenly reports ``SchemaIncompatible`` (report P2-2).
MIGRATION_19_CHECKSUM = "a50c5eaf2d4a137265623670ad39affa184473e6e3fd10e654a3ca28f48812e0"

NEW_TABLES = ("mission_planning_protocols", "planning_requests", "planning_decisions")


# --------------------------------------------------------------------------------------
# Builders and fixtures
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


def request_binding(
    *,
    request_id: str = "request-1",
    base_plan_revision: int = 3,
    created_at: float = 10.0,
    package_hash: str = HASH_A,
    prompt_hash: str = HASH_B,
) -> PlanningRequestBinding:
    return PlanningRequestBinding(
        request_id=request_id,
        mission_id=MISSION,
        protocol_version=PROTOCOL,
        package_version=PACKAGE_VERSION,
        package_hash=package_hash,
        base_plan_revision=base_plan_revision,
        requirements_revision=2,
        scope_epoch_digest=HASH_C,
        subject_bindings_hash=HASH_D,
        visible_refs_digest=HASH_E,
        prompt_version=PROMPT,
        prompt_hash=prompt_hash,
        created_at=created_at,
        intent_id="intent-1",
    )


@pytest.fixture
def store(tmp_path) -> Store:
    opened = Store.open(tmp_path / "orchestrator.db")
    opened.insert_mission(_mission(), spec_hash="h")
    return opened


@pytest.fixture
def decisions(store: Store) -> PlanningDecisionStore:
    return PlanningDecisionStore(store)


def _bind(decisions: PlanningDecisionStore, mission_id: str = MISSION) -> None:
    decisions.bind_mission_protocol(
        mission_id,
        protocol_version=PROTOCOL,
        package_version=PACKAGE_VERSION,
        prompt_version=PROMPT,
        binding_hash=HASH_A,
    )


def record(
    decisions: PlanningDecisionStore,
    *,
    request_id: str = "request-1",
    attempt_ordinal: int = 0,
    raw_output_hash: str = HASH_A,
    status: PlanningDecisionStatus = PlanningDecisionStatus.DECODED,
    decision_type: str | None = "REFINE",
    rejection_codes: tuple[str, ...] = (),
    detail: dict[str, Any] | None = None,
    canonical_json: str | None = None,
    canonical_hash: str | None = None,
    raw_artifact_ref: str | None = None,
    decision_id: str | None = None,
) -> dict[str, Any]:
    return decisions.record_planning_decision(
        request_id=request_id,
        attempt_ordinal=attempt_ordinal,
        raw_output_hash=raw_output_hash,
        decision_id=(
            compute_decision_id(request_id, attempt_ordinal, raw_output_hash)
            if decision_id is None
            else decision_id
        ),
        status=status,
        rejection_codes=rejection_codes,
        detail={"reason": "ok"} if detail is None else detail,
        raw_artifact_ref=raw_artifact_ref,
        canonical_json=canonical_json,
        canonical_hash=canonical_hash,
        decision_type=decision_type,
    )


def _tables(store: Store) -> dict[str, str]:
    return {
        row[0]: row[1]
        for row in store.connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='table'"
        )
    }


def _decision_rows(store: Store) -> list[tuple[Any, ...]]:
    return [
        tuple(row)
        for row in store.connection.execute(
            "SELECT decision_id, request_id, attempt_ordinal, raw_output_hash, status"
            " FROM planning_decisions ORDER BY attempt_ordinal"
        )
    ]


# --------------------------------------------------------------------------------------
# S1: migration 19 is the head, and it is one migration with three STRICT tables
# --------------------------------------------------------------------------------------


def test_migration_nineteen_is_the_new_head() -> None:
    assert schema.SCHEMA_VERSION == 19
    assert schema.SCHEMA_NAME == "orchestrator-planning-decision-v1"
    assert schema.MIGRATIONS[-1].ddl is planning_decision_schema.DDL
    assert schema.MIGRATIONS[18].checksum == MIGRATION_19_CHECKSUM
    assert schema.checksum() == MIGRATION_19_CHECKSUM


def test_a_fresh_library_has_the_three_strict_tables(store: Store) -> None:
    """S1: the DDL is one migration and every table it adds is STRICT."""

    assert planning_decision_schema.TABLES == NEW_TABLES
    rows = _tables(store)
    for table in NEW_TABLES:
        assert table in rows, table
        assert "STRICT" in rows[table].upper(), table
    assert set(planning_decision_schema.DDL.split("CREATE TABLE")) - {""}


#: Migration 19 as shipped.  ``(name, declared type, NOT NULL, PRIMARY KEY)`` per
#: column, in ``PRAGMA table_info`` order.  Like 16/17/18, the DDL is frozen: dropping a
#: ``NOT NULL`` (or a PK, or changing a type) would silently change the migration's
#: checksum and make every deployed library report ``SchemaIncompatible``.
PROTOCOL_SCHEMA: dict[str, tuple[tuple[str, str, bool, bool], ...]] = {
    "mission_planning_protocols": (
        ("mission_id", "TEXT", True, True),
        ("protocol_version", "TEXT", True, False),
        ("package_version", "INTEGER", True, False),
        ("prompt_version", "TEXT", True, False),
        ("binding_hash", "TEXT", True, False),
        ("created_at", "REAL", True, False),
    ),
    "planning_requests": (
        ("request_id", "TEXT", True, True),
        ("mission_id", "TEXT", True, False),
        ("protocol_version", "TEXT", True, False),
        ("package_version", "INTEGER", True, False),
        ("package_hash", "TEXT", True, False),
        ("base_plan_revision", "INTEGER", True, False),
        ("requirements_revision", "INTEGER", True, False),
        ("scope_epoch_digest", "TEXT", True, False),
        ("subject_bindings_hash", "TEXT", True, False),
        ("visible_refs_digest", "TEXT", True, False),
        ("prompt_version", "TEXT", True, False),
        ("prompt_hash", "TEXT", True, False),
        ("intent_id", "TEXT", True, False),
        ("created_at", "REAL", True, False),
    ),
    "planning_decisions": (
        ("decision_id", "TEXT", True, True),
        ("request_id", "TEXT", True, False),
        ("attempt_ordinal", "INTEGER", True, False),
        ("raw_output_hash", "TEXT", True, False),
        ("raw_artifact_ref", "TEXT", False, False),
        ("canonical_json", "TEXT", False, False),
        ("canonical_hash", "TEXT", False, False),
        ("decision_type", "TEXT", False, False),
        ("status", "TEXT", True, False),
        ("rejection_codes_json", "TEXT", True, False),
        ("detail_json", "TEXT", True, False),
        ("created_at", "REAL", True, False),
    ),
}


def test_the_protocol_columns_are_exactly_the_section_eight_dot_two_columns(store: Store) -> None:
    """The DDL is copied verbatim: names, order, types, NOT NULL and PK are the contract.

    Pinning only the names would let a dropped ``NOT NULL`` through (report P1-1).
    """

    for table, expected in PROTOCOL_SCHEMA.items():
        rows = [
            (row[1], row[2].upper(), bool(row[3]), bool(row[5]))
            for row in store.connection.execute(f"PRAGMA table_info({table})")
        ]
        assert rows == list(expected), table


# --------------------------------------------------------------------------------------
# S2: migrations 1–18 are untouched
# --------------------------------------------------------------------------------------


def test_migrations_sixteen_seventeen_and_eighteen_keep_their_checksums() -> None:
    """S2: the new head does not move, rename or re-number any old migration."""

    sixteen, seventeen, eighteen = (
        schema.MIGRATIONS[15],
        schema.MIGRATIONS[16],
        schema.MIGRATIONS[17],
    )
    assert (sixteen.version, sixteen.name) == (16, "orchestrator-full-target-htn")
    assert sixteen.checksum == FROZEN_16_CHECKSUM
    assert (seventeen.version, seventeen.name) == (
        17,
        "orchestrator-full-target-acceptance-receipts",
    )
    assert seventeen.checksum == FROZEN_17_CHECKSUM
    assert (eighteen.version, eighteen.name) == (
        18,
        "orchestrator-full-target-witness-subject",
    )
    assert eighteen.checksum == FROZEN_18_CHECKSUM


# --------------------------------------------------------------------------------------
# S3–S5: the mission protocol binding, written once and never switched
# --------------------------------------------------------------------------------------


def test_get_mission_protocol_is_none_for_a_mission_without_a_row(
    decisions: PlanningDecisionStore,
) -> None:
    """S3: no row is legacy, not an error and not a guess."""

    assert decisions.get_mission_protocol(MISSION) is None


def test_bind_mission_protocol_round_trips_and_is_idempotent(
    decisions: PlanningDecisionStore,
) -> None:
    """S4: the same binding written twice is the same binding, not a conflict."""

    _bind(decisions)
    stored = decisions.get_mission_protocol(MISSION)
    assert stored is not None
    assert stored["mission_id"] == MISSION
    assert stored["protocol_version"] == PROTOCOL
    assert stored["package_version"] == PACKAGE_VERSION
    assert stored["prompt_version"] == PROMPT
    assert stored["binding_hash"] == HASH_A
    _bind(decisions)  # idempotent: an identical rebind is a no-op
    assert decisions.get_mission_protocol(MISSION) == stored


def test_a_mission_may_not_switch_protocol(decisions: PlanningDecisionStore) -> None:
    """S5: the second, different binding is refused and the first one survives."""

    _bind(decisions)
    with pytest.raises(StoreConflict):
        decisions.bind_mission_protocol(
            MISSION,
            protocol_version="some-other-protocol-v2",
            package_version=PACKAGE_VERSION,
            prompt_version=PROMPT,
            binding_hash=HASH_A,
        )
    with pytest.raises(StoreConflict):
        decisions.bind_mission_protocol(
            MISSION,
            protocol_version=PROTOCOL,
            package_version=PACKAGE_VERSION,
            prompt_version=PROMPT,
            binding_hash=HASH_B,
        )
    stored = decisions.get_mission_protocol(MISSION)
    assert stored is not None
    assert (stored["protocol_version"], stored["binding_hash"]) == (PROTOCOL, HASH_A)


def test_a_new_mission_does_not_inherit_a_binding(decisions: PlanningDecisionStore) -> None:
    """A new mission_id starts as legacy; there is no clone API (§8.2)."""

    _bind(decisions)
    assert decisions.get_mission_protocol("mission-2") is None


# --------------------------------------------------------------------------------------
# S6: the request binding round-trips
# --------------------------------------------------------------------------------------


def test_a_planning_request_round_trips(decisions: PlanningDecisionStore) -> None:
    """S6: every §34/§36 field survives, ``intent_id`` included."""

    binding = request_binding()
    stored = decisions.insert_planning_request(binding)
    assert stored == binding
    assert decisions.get_planning_request("request-1") == binding
    assert decisions.get_planning_request("absent") is None


def test_the_same_request_content_is_idempotent_and_a_different_one_conflicts(
    decisions: PlanningDecisionStore,
) -> None:
    """Identity conflicts are named, not left as a raw ``sqlite3.IntegrityError``."""

    first = decisions.insert_planning_request(request_binding())
    assert decisions.insert_planning_request(request_binding()) == first
    with pytest.raises(StoreConflict):
        decisions.insert_planning_request(request_binding(base_plan_revision=4))
    assert decisions.get_planning_request("request-1") == first


# --------------------------------------------------------------------------------------
# S7–S9: one decision per attempt, with the raw output as its identity
# --------------------------------------------------------------------------------------


def test_replaying_the_same_raw_output_returns_the_same_row(
    store: Store, decisions: PlanningDecisionStore
) -> None:
    """S7: same request + ordinal + raw is a replay: one row, no second event row."""

    decisions.insert_planning_request(request_binding())
    first = record(
        decisions,
        status=PlanningDecisionStatus.DECODED,
        canonical_json='{"decision_type":"REFINE"}',
        canonical_hash=HASH_C,
    )
    replay = record(
        decisions,
        status=PlanningDecisionStatus.DECODED,
        canonical_json='{"decision_type":"REFINE"}',
        canonical_hash=HASH_C,
    )
    assert replay == first
    assert _decision_rows(store) == [
        (first["decision_id"], "request-1", 0, HASH_A, str(PlanningDecisionStatus.DECODED))
    ]
    assert decisions.get_planning_decision(first["decision_id"]) == first
    assert decisions.get_planning_decision_by_attempt("request-1", 0) == first
    assert decisions.get_planning_decision_by_attempt("request-1", 1) is None


def test_the_same_ordinal_with_a_different_raw_output_is_an_identity_conflict(
    store: Store, decisions: PlanningDecisionStore
) -> None:
    """S8/M1: a second attempt is a new ordinal; it is never an UPDATE of this one."""

    decisions.insert_planning_request(request_binding())
    first = record(decisions)
    with pytest.raises(StoreConflict):
        record(decisions, raw_output_hash=HASH_B)
    # The raw output is the identity on its own: even a caller that hands back the
    # first decision_id may not attach it to different bytes.
    with pytest.raises(StoreConflict):
        record(
            decisions,
            raw_output_hash=HASH_B,
            decision_id=first["decision_id"],
        )
    assert _decision_rows(store) == [
        (first["decision_id"], "request-1", 0, HASH_A, str(PlanningDecisionStatus.DECODED))
    ]


def test_the_unique_attempt_index_is_enforced_by_sqlite(
    store: Store, decisions: PlanningDecisionStore
) -> None:
    """S9: the second line of defence is the schema, not the store's good manners."""

    unique_keys = []
    listing = list(store.connection.execute("PRAGMA index_list(planning_decisions)"))
    for row in listing:
        name, unique = row[1], row[2]
        if not unique:
            continue
        columns = sorted(
            ((row[0], row[2]) for row in store.connection.execute(f"PRAGMA index_info({name})")),
        )
        unique_keys.append(tuple(column for _, column in columns))
    assert ("request_id", "attempt_ordinal") in unique_keys

    decisions.insert_planning_request(request_binding())
    record(decisions)
    with pytest.raises(sqlite3.IntegrityError):
        with store.transaction() as connection:
            connection.execute(
                "INSERT INTO planning_decisions(decision_id,request_id,attempt_ordinal,"
                "raw_output_hash,status,rejection_codes_json,detail_json,created_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                ("pd-other", "request-1", 0, HASH_B, "DECODED", "[]", "{}", 1.0),
            )


def test_the_store_writes_inside_the_callers_transaction(
    store: Store, decisions: PlanningDecisionStore
) -> None:
    """§8.2: the protocol row and the Mission creation are one business transaction.

    The store composes an open ``Store.transaction()`` instead of opening its own, so
    a caller may bind the protocol and create the Mission atomically.  The proof is
    that an exception after the binding rolls the binding back with the rest.
    """

    assert decisions.get_mission_protocol(MISSION) is None
    with pytest.raises(RuntimeError):
        with store.transaction():
            _bind(decisions)
            decisions.insert_planning_request(request_binding())
            record(decisions)
            assert decisions.get_mission_protocol(MISSION) is not None
            raise RuntimeError("the business transaction failed after the binding")
    assert decisions.get_mission_protocol(MISSION) is None
    assert decisions.get_planning_request("request-1") is None
    assert decisions.get_planning_decision_by_attempt("request-1", 0) is None


# --------------------------------------------------------------------------------------
# S10: a crash between the insert and the commit leaves no half row
# --------------------------------------------------------------------------------------


def test_a_crash_inside_the_transaction_leaves_no_half_row(
    store: Store, decisions: PlanningDecisionStore
) -> None:
    """S10: the decision row and everything beside it land together or not at all."""

    decisions.insert_planning_request(request_binding())
    store.arm("h1b-decision")
    with pytest.raises(InjectedCrash):
        with store.transaction():
            record(decisions, status=PlanningDecisionStatus.ADMITTED)
            store.fault("h1b-decision")
    assert _decision_rows(store) == []
    assert decisions.get_planning_decision_by_attempt("request-1", 0) is None

    committed = record(decisions, status=PlanningDecisionStatus.ADMITTED)
    assert decisions.get_planning_decision_by_attempt("request-1", 0) == committed


# --------------------------------------------------------------------------------------
# §36 status progression: forward only, never out of a terminal state
# --------------------------------------------------------------------------------------


def test_status_advances_forward_on_the_same_row(
    store: Store, decisions: PlanningDecisionStore
) -> None:
    decisions.insert_planning_request(request_binding())
    decoded = record(decisions, status=PlanningDecisionStatus.DECODED)
    admitted = record(
        decisions,
        status=PlanningDecisionStatus.ADMITTED,
        canonical_json='{"decision_type":"REFINE"}',
        canonical_hash=HASH_C,
    )
    assert admitted["decision_id"] == decoded["decision_id"]
    assert admitted["status"] == str(PlanningDecisionStatus.ADMITTED)
    compiled = record(decisions, status=PlanningDecisionStatus.COMPILED)
    assert compiled["status"] == str(PlanningDecisionStatus.COMPILED)
    assert len(_decision_rows(store)) == 1


def test_a_forward_step_does_not_erase_evidence(
    store: Store, decisions: PlanningDecisionStore
) -> None:
    """Advancing the status is not a licence to blank the evaluation columns.

    A row is one attempt, and the steps of one attempt see the same evaluation.  A
    later call that carries no detail (or an empty rejection list) therefore leaves
    what the earlier step stored: the store fills in what it learns and never blanks a
    column (report P2-1).
    """

    decisions.insert_planning_request(request_binding())
    decoded = record(
        decisions,
        status=PlanningDecisionStatus.DECODED,
        detail={"stage": "decode", "rich": "value"},
        rejection_codes=("DECISION_BLOCK_MISSING",),
    )
    # The step that has nothing new to say says nothing: an empty object and an empty
    # list mean "no new information", not "erase what is there".
    advanced = record(decisions, status=PlanningDecisionStatus.ADMITTED, detail={})
    assert advanced["decision_id"] == decoded["decision_id"]
    assert advanced["status"] == "ADMITTED"
    assert advanced["detail"] == {"stage": "decode", "rich": "value"}
    assert advanced["rejection_codes"] == ["DECISION_BLOCK_MISSING"]
    assert decisions.get_planning_decision_by_attempt("request-1", 0) == advanced


def test_a_forward_step_may_replace_the_evaluation_when_it_has_new_detail(
    store: Store, decisions: PlanningDecisionStore
) -> None:
    """Filling in is allowed to overwrite with real information, not to empty a column."""

    decisions.insert_planning_request(request_binding())
    record(decisions, status=PlanningDecisionStatus.DECODED, detail={"stage": "decode"})
    advanced = record(
        decisions,
        status=PlanningDecisionStatus.ADMITTED,
        detail={"stage": "admit"},
        rejection_codes=("EVIDENCE_REQUIRED",),
    )
    assert advanced["detail"] == {"stage": "admit"}
    assert advanced["rejection_codes"] == ["EVIDENCE_REQUIRED"]


def test_every_terminal_status_freezes_the_row(
    store: Store, decisions: PlanningDecisionStore
) -> None:
    """§36: the five statuses that end an attempt all absorb; none is walkable out of."""

    terminal = {
        PlanningDecisionStatus.UNREADABLE,
        PlanningDecisionStatus.REJECTED,
        PlanningDecisionStatus.COMMIT_REJECTED,
        PlanningDecisionStatus.COMMITTED,
        PlanningDecisionStatus.NO_STATE_CHANGE,
    }
    for index, status in enumerate(sorted(terminal, key=str)):
        request_id = f"request-{index}"
        decisions.insert_planning_request(request_binding(request_id=request_id))
        record(
            decisions,
            request_id=request_id,
            status=status,
            rejection_codes=("MALFORMED_DECISION",),
        )
        for target in (PlanningDecisionStatus.ADMITTED, PlanningDecisionStatus.COMPILED):
            with pytest.raises(StoreConflict):
                record(decisions, request_id=request_id, status=target)
        assert (
            decisions.get_planning_decision_by_attempt(request_id, 0)["status"] == str(status)
        )


def test_status_never_goes_backwards(store: Store, decisions: PlanningDecisionStore) -> None:
    decisions.insert_planning_request(request_binding())
    record(decisions, status=PlanningDecisionStatus.ADMITTED)
    with pytest.raises(StoreConflict):
        record(decisions, status=PlanningDecisionStatus.DECODED)
    assert decisions.get_planning_decision_by_attempt("request-1", 0)["status"] == "ADMITTED"


def test_a_terminal_status_is_absorbing(store: Store, decisions: PlanningDecisionStore) -> None:
    """Terminal means terminal: REJECTED / COMMIT_REJECTED / COMMITTED / NO_STATE_CHANGE."""

    decisions.insert_planning_request(request_binding())
    record(
        decisions,
        status=PlanningDecisionStatus.REJECTED,
        rejection_codes=("MALFORMED_DECISION",),
    )
    for status in (PlanningDecisionStatus.ADMITTED, PlanningDecisionStatus.DECODED):
        with pytest.raises(StoreConflict):
            record(decisions, status=status)
    assert decisions.get_planning_decision_by_attempt("request-1", 0)["status"] == "REJECTED"


def test_the_status_column_only_accepts_the_eight_values(
    decisions: PlanningDecisionStore,
) -> None:
    decisions.insert_planning_request(request_binding())
    with pytest.raises(ContractError):
        record(decisions, status="GARBAGE")


def test_rejection_codes_are_restricted_to_the_rejection_code_enum(
    decisions: PlanningDecisionStore,
) -> None:
    decisions.insert_planning_request(request_binding())
    with pytest.raises(ContractError):
        record(
            decisions,
            status=PlanningDecisionStatus.REJECTED,
            rejection_codes=("NOPE",),
        )
    stored = record(
        decisions,
        status=PlanningDecisionStatus.REJECTED,
        rejection_codes=("MALFORMED_DECISION", "REQUEST_BINDING_STALE"),
    )
    assert stored["rejection_codes"] == ["MALFORMED_DECISION", "REQUEST_BINDING_STALE"]


# --------------------------------------------------------------------------------------
# Foreign keys: a decision belongs to a recorded request
# --------------------------------------------------------------------------------------


def test_a_decision_needs_a_recorded_request(
    store: Store, decisions: PlanningDecisionStore
) -> None:
    with pytest.raises(StoreConflict):
        record(decisions, request_id="never-inserted")
    with pytest.raises(sqlite3.IntegrityError):
        with store.transaction() as connection:
            connection.execute(
                "INSERT INTO planning_decisions(decision_id,request_id,attempt_ordinal,"
                "raw_output_hash,status,rejection_codes_json,detail_json,created_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                ("pd-orphan", "never-inserted", 0, HASH_A, "DECODED", "[]", "{}", 1.0),
            )
    assert _decision_rows(store) == []


# --------------------------------------------------------------------------------------
# Upgrade path: 18 → 19 keeps every old row and leaves the new tables empty
# --------------------------------------------------------------------------------------


def test_an_upgrade_from_eighteen_keeps_every_old_row(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "deployed.db"
    monkeypatch.setattr(schema, "MIGRATIONS", schema.MIGRATIONS[:18])
    older = Store.open(path)
    older.insert_mission(_mission(), spec_hash="h")
    older.close()
    monkeypatch.undo()
    before = list(
        sqlite3.connect(path).execute("SELECT mission_id, spec_hash FROM missions ORDER BY 1")
    )
    assert before

    upgraded = Store.open(path)
    try:
        applied = [
            tuple(row)
            for row in upgraded.connection.execute(
                "SELECT version,name,checksum FROM orch_schema_migrations ORDER BY version"
            )
        ]
        assert [row[0] for row in applied] == list(range(1, 20))
        assert applied[-1] == (19, schema.SCHEMA_NAME, schema.MIGRATIONS[-1].checksum)
        assert applied[-2] == (18, "orchestrator-full-target-witness-subject", FROZEN_18_CHECKSUM)
        assert (tmp_path / "deployed.db.pre-schema-19.backup").is_file()
        rows = _tables(upgraded)
        for table in NEW_TABLES:
            assert table in rows and "STRICT" in rows[table].upper(), table
        assert (
            list(
                sqlite3.connect(path).execute(
                    "SELECT mission_id, spec_hash FROM missions ORDER BY 1"
                )
            )
            == before
        )
    finally:
        upgraded.close()


def test_a_legacy_run_writes_nothing_into_the_new_tables(tmp_path) -> None:
    """The legacy path is untouched: not one row in any of the three tables."""

    spec = importlib.util.spec_from_file_location("step02_store_and_budgets", STEP02_TESTS)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.test_store_opens_validates_and_reopens(tmp_path)
    module.test_cas_and_idempotent_events(tmp_path)
    module.test_budget_chain_reserve_settle_and_unpriced(tmp_path)

    legacy = Store.open(tmp_path / "o.db")
    try:
        assert legacy.count_events("mission-1") == 1
        for table in NEW_TABLES:
            count = legacy.connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]  # noqa: S608
            assert count == 0, table
        assert PlanningDecisionStore(legacy).get_mission_protocol("mission-1") is None
    finally:
        legacy.close()


def test_an_unreadable_decision_may_have_no_canonical_bytes(
    decisions: PlanningDecisionStore,
) -> None:
    """§13/BL-9: UNREADABLE has no decision text, so the three columns stay NULL."""

    decisions.insert_planning_request(request_binding())
    stored = record(
        decisions,
        status=PlanningDecisionStatus.UNREADABLE,
        decision_type=None,
        rejection_codes=("MALFORMED_DECISION", "DECISION_BLOCK_MISSING"),
        detail={"stage": "decode"},
    )
    assert stored["canonical_json"] is None
    assert stored["canonical_hash"] is None
    assert stored["decision_type"] is None
    assert stored["raw_artifact_ref"] is None
    assert sorted(stored["rejection_codes"]) == ["DECISION_BLOCK_MISSING", "MALFORMED_DECISION"]
    assert decisions.get_planning_decision(stored["decision_id"]) == stored


def test_the_learned_columns_are_stored_verbatim(decisions: PlanningDecisionStore) -> None:
    """§15: the store keeps the caller's canonical bytes; the hash describes them."""

    decisions.insert_planning_request(request_binding())
    text = '{"decision_type":"REFINE","rationale":"hello"}'
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    stored = record(
        decisions,
        status=PlanningDecisionStatus.ADMITTED,
        canonical_json=text,
        canonical_hash=digest,
        raw_artifact_ref="artifact-1",
    )
    assert stored["canonical_json"] == text
    assert stored["canonical_hash"] == digest
    assert stored["decision_type"] == "REFINE"
    assert stored["raw_artifact_ref"] == "artifact-1"
    assert decisions.get_planning_decision(stored["decision_id"]) == stored


def test_non_json_canonical_bytes_are_refused(decisions: PlanningDecisionStore) -> None:
    decisions.insert_planning_request(request_binding())
    with pytest.raises(ContractError):
        record(decisions, canonical_json="not json")
    assert decisions.get_planning_decision_by_attempt("request-1", 0) is None


def test_a_second_decision_id_for_the_same_attempt_is_a_conflict(
    decisions: PlanningDecisionStore,
) -> None:
    """The attempt already has an identity; a second one is not a new decision."""

    decisions.insert_planning_request(request_binding())
    first = record(decisions)
    with pytest.raises(StoreConflict):
        record(decisions, decision_id="pd-something-else")
    assert decisions.get_planning_decision_by_attempt("request-1", 0)["decision_id"] == (
        first["decision_id"]
    )


def test_the_record_decodes_its_json_columns(
    decisions: PlanningDecisionStore,
) -> None:
    decisions.insert_planning_request(request_binding())
    stored = record(
        decisions,
        status=PlanningDecisionStatus.REJECTED,
        rejection_codes=("MALFORMED_DECISION",),
        detail={"stage": "decode"},
    )
    assert json.dumps(stored["detail"], sort_keys=True) == json.dumps(
        {"stage": "decode"}, sort_keys=True
    )
    assert stored["rejection_codes"] == ["MALFORMED_DECISION"]
