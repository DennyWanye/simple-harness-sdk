# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""The durable twin of :class:`~agent_orchestrator.contracts.obligations.ObligationLedger`.

The in-memory ledger fixes the *arithmetic* of a duty (§6.1, §6.4): failures,
spend and recursion fuel are keyed on ``obligation_id`` alone, and the operations
that change the shape of the work — renaming a task, swapping a method, handing
it to another agent — are recorded as history without moving a counter.  This
module writes exactly the same arithmetic to ``orchestrator.db``.

Two rules are load-bearing here and are tested:

* **Validate before writing.**  Every argument is checked with the same contract
  validators the ledger uses *before* any statement runs, so a refused call leaves
  the row byte-for-byte as it was.  A caller that retries after a rejected call
  must not find half of it applied.
* **Nothing resets a counter.**  Only an explicitly new ``obligation_id`` starts a
  fresh allowance; :meth:`ObligationStore.persist` never lowers a stored counter
  that the ledger does not track (``spent_tokens``).

The store owns no connection: it composes an existing :class:`Store` and runs
inside that store's ``transaction()``, so an obligation write and the plan write
beside it either both land or both roll back.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from simple_harness.contracts import canonical_json

from ..contracts.htn import ObligationId, ObligationRelation
from ..contracts.htn import obligation_id as _obligation_id
from ..contracts.obligations import (
    ExpansionRecord,
    FuelDecision,
    FuelStatus,
    Obligation,
    ObligationAccountView,
    ObligationLedger,
    ObligationLifecycle,
    ShapeChange,
)
from ..contracts.semantic_base import enum_of, identifier, index, optional_identifier, text
from .store import Store, StoreConflict

DEFAULT_RECURSION_FUEL = 3


@dataclass(frozen=True, slots=True)
class ObligationRelationRow:
    """One ``parent → child`` duty relation as it is stored (§6.5)."""

    mission_id: str
    parent_obligation_id: ObligationId
    child_obligation_id: ObligationId
    kind: ObligationRelation
    active_revision: int
    detail: dict[str, Any]


class ObligationStore:
    """Persistent obligations, their counters, fuel and refinement history."""

    def __init__(self, store: Store) -> None:
        self._store = store

    # ------------------------------------------------------------------ registration
    def register(self, obligation: Obligation, *, recursion_fuel: int | None = None) -> None:
        """Add a duty.  Re-registering the same id is refused, never re-opened."""

        if not isinstance(obligation, Obligation):
            raise StoreConflict("register expects an Obligation")
        fuel = (
            DEFAULT_RECURSION_FUEL
            if recursion_fuel is None
            else index(recursion_fuel, "recursion_fuel")
        )
        document = obligation.to_json()
        now = self._store.now
        with self._store.transaction() as connection:
            try:
                connection.execute(
                    "INSERT INTO obligations(mission_id,obligation_id,goal_signature_id,scope,"
                    "requiredness,lifecycle,resolution_ref,parent_obligation_id,budget_lineage_ref,"
                    "failure_count,spent_tokens,spent_cost_micros,spent_attempts,fuel_limit,"
                    "fuel_used,fuel_remaining,obligation_json,created_at,updated_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,0,0,0,0,?,0,?,?,?,?)",
                    (
                        obligation.mission_id,
                        str(obligation.obligation_id),
                        obligation.goal_signature_id,
                        obligation.scope,
                        str(obligation.requiredness),
                        str(obligation.lifecycle),
                        obligation.resolution_ref,
                        (
                            None
                            if obligation.parent_obligation_id is None
                            else str(obligation.parent_obligation_id)
                        ),
                        obligation.budget_lineage_ref,
                        fuel,
                        fuel,
                        canonical_json(document),
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise StoreConflict(
                    f"obligation {obligation.obligation_id!s} is already registered; "
                    f"a genuinely new duty needs a new obligation_id (§6.1): {error}"
                ) from error

    def obligation(self, mission_id: str, target: ObligationId) -> Obligation:
        return Obligation.from_json(json.loads(self._row(mission_id, target)["obligation_json"]))

    def obligation_ids(self, mission_id: str) -> tuple[ObligationId, ...]:
        rows = self._store.connection.execute(
            "SELECT obligation_id FROM obligations WHERE mission_id = ? ORDER BY created_at,"
            " obligation_id",
            (identifier(mission_id, "mission_id"),),
        ).fetchall()
        return tuple(ObligationId(row[0]) for row in rows)

    def list_obligations(self, mission_id: str) -> tuple[Obligation, ...]:
        rows = self._store.connection.execute(
            "SELECT obligation_json FROM obligations WHERE mission_id = ?"
            " ORDER BY created_at, obligation_id",
            (identifier(mission_id, "mission_id"),),
        ).fetchall()
        return tuple(Obligation.from_json(json.loads(row[0])) for row in rows)

    def account(self, mission_id: str, target: ObligationId) -> ObligationAccountView:
        row = self._row(mission_id, target)
        return ObligationAccountView(
            obligation_id=ObligationId(row["obligation_id"]),
            failure_count=int(row["failure_count"]),
            consumed_cost_micros=int(row["spent_cost_micros"]),
            consumed_attempts=int(row["spent_attempts"]),
            consumed_tokens=int(row["spent_tokens"]),
            has_admitted_demand=bool(row["demand_admitted"]),
            fuel_limit=int(row["fuel_limit"]),
            fuel_used=int(row["fuel_used"]),
            expansions=self._count(
                "obligation_expansions", row["mission_id"], row["obligation_id"]
            ),
            shape_changes=self._count(
                "obligation_shape_changes", row["mission_id"], row["obligation_id"]
            ),
            lifecycle=ObligationLifecycle(row["lifecycle"]),
            resolution_ref=row["resolution_ref"],
        )

    def exists(self, mission_id: str, target: ObligationId) -> bool:
        """Whether this Mission carries this duty — the membership check a commit
        path needs, so it does not have to spell the table name in SQL of its own."""

        row = self._store.connection.execute(
            "SELECT 1 FROM obligations WHERE mission_id = ? AND obligation_id = ?",
            (identifier(mission_id, "mission_id"), str(_obligation_id(target, "obligation_id"))),
        ).fetchone()
        return row is not None

    def spent_tokens(self, mission_id: str, target: ObligationId) -> int:
        """The token axis, read on its own.  Same value as ``account().consumed_tokens``."""

        return int(self._row(mission_id, target)["spent_tokens"])

    # ------------------------------------------------------------------ accumulation
    def record_failure(self, mission_id: str, target: ObligationId, *, count: int = 1) -> int:
        """§6.1: failures accrue against the duty, not against a task name."""

        steps = index(count, "count", minimum=1)
        mission = identifier(mission_id, "mission_id")
        duty = str(_obligation_id(target, "obligation_id"))
        # Relative, inside the write transaction: reading the count first and writing
        # the sum back would lose a concurrent writer's failure — exactly the kind of
        # "the retry allowance looks fresh" bug §6.1 exists to prevent.
        with self._store.transaction() as connection:
            cursor = connection.execute(
                "UPDATE obligations SET failure_count = failure_count + ?, updated_at = ?"
                " WHERE mission_id = ? AND obligation_id = ?",
                (steps, self._store.now, mission, duty),
            )
            if cursor.rowcount == 0:
                raise StoreConflict(f"obligation {duty} is not registered in mission {mission}")
            row = connection.execute(
                "SELECT failure_count FROM obligations WHERE mission_id = ? AND obligation_id = ?",
                (mission, duty),
            ).fetchone()
        return int(row[0])

    def record_spend(
        self,
        mission_id: str,
        target: ObligationId,
        *,
        cost_micros: int = 0,
        attempts: int = 0,
        tokens: int = 0,
    ) -> ObligationAccountView:
        """Accumulate spend.  Every argument is validated before the row is touched."""

        spent = index(cost_micros, "cost_micros")
        tries = index(attempts, "attempts")
        used_tokens = index(tokens, "tokens")
        mission = identifier(mission_id, "mission_id")
        duty = str(_obligation_id(target, "obligation_id"))
        with self._store.transaction() as connection:
            cursor = connection.execute(
                "UPDATE obligations SET spent_cost_micros = spent_cost_micros + ?,"
                " spent_attempts = spent_attempts + ?, spent_tokens = spent_tokens + ?,"
                " updated_at = ? WHERE mission_id = ? AND obligation_id = ?",
                (spent, tries, used_tokens, self._store.now, mission, duty),
            )
            if cursor.rowcount == 0:
                raise StoreConflict(f"obligation {duty} is not registered in mission {mission}")
            return self.account(mission, target)

    def note_shape_change(
        self, mission_id: str, target: ObligationId, change: ShapeChange, *, detail: str
    ) -> ObligationAccountView:
        """Record that the work changed shape.  Counters are deliberately untouched."""

        kind = enum_of(ShapeChange, change, "shape_change")
        note = text(detail, "detail", limit=512)
        mission = identifier(mission_id, "mission_id")
        duty = str(_obligation_id(target, "obligation_id"))
        # The ordinal is read *inside* the write transaction: taking it outside would
        # hand two concurrent writers the same number and lose one of the histories.
        with self._store.transaction() as connection:
            self._require_row(connection, mission, duty)
            ordinal = self._next_ordinal(connection, "obligation_shape_changes", mission, duty)
            try:
                connection.execute(
                    "INSERT INTO obligation_shape_changes(mission_id,obligation_id,ordinal,change,"
                    "detail,created_at) VALUES (?,?,?,?,?,?)",
                    (mission, duty, ordinal, str(kind), note, self._store.now),
                )
            except sqlite3.IntegrityError as error:
                raise StoreConflict(f"shape change for {duty} was rejected: {error}") from error
            return self.account(mission, target)

    def shape_changes(
        self, mission_id: str, target: ObligationId
    ) -> tuple[tuple[ShapeChange, str], ...]:
        row = self._row(mission_id, target)
        rows = self._store.connection.execute(
            "SELECT change, detail FROM obligation_shape_changes"
            " WHERE mission_id = ? AND obligation_id = ? ORDER BY ordinal",
            (row["mission_id"], row["obligation_id"]),
        ).fetchall()
        return tuple((ShapeChange(item[0]), str(item[1])) for item in rows)

    def set_lifecycle(
        self,
        mission_id: str,
        target: ObligationId,
        lifecycle: ObligationLifecycle,
        *,
        resolution_ref: str | None = None,
    ) -> ObligationAccountView:
        state = enum_of(ObligationLifecycle, lifecycle, "lifecycle")
        reference = optional_identifier(resolution_ref, "resolution_ref")
        mission = identifier(mission_id, "mission_id")
        duty = str(_obligation_id(target, "obligation_id"))
        with self._store.transaction() as connection:
            row = self._require_row(connection, mission, duty)
            stored = row["resolution_ref"] if reference is None else reference
            if state is ObligationLifecycle.SATISFIED and stored is None:
                raise StoreConflict("a SATISFIED obligation needs a resolution_ref")
            document = json.loads(row["obligation_json"])
            document["lifecycle"] = str(state)
            document["resolution_ref"] = stored
            try:
                connection.execute(
                    "UPDATE obligations SET lifecycle = ?, resolution_ref = ?, obligation_json = ?,"
                    " updated_at = ? WHERE mission_id = ? AND obligation_id = ?",
                    (str(state), stored, canonical_json(document), self._store.now, mission, duty),
                )
            except sqlite3.IntegrityError as error:
                # The open-duty CHECK: an admitted demand may not be left hanging on a
                # duty that is no longer open (TG decision 9).
                raise StoreConflict(
                    f"obligation {duty} may not move to {state!s} in its current state: {error}"
                ) from error
            return self.account(mission, target)

    # ------------------------------------------------------------------ shared demand
    def admit_demand(self, mission_id: str, target: ObligationId) -> ObligationAccountView:
        """TG decision 9: record that a live consumer is sharing this duty's work."""

        mission = identifier(mission_id, "mission_id")
        duty = str(_obligation_id(target, "obligation_id"))
        with self._store.transaction() as connection:
            row = self._require_row(connection, mission, duty)
            if ObligationLifecycle(row["lifecycle"]) is not ObligationLifecycle.UNSATISFIED:
                raise StoreConflict("a demand may only be admitted against an open obligation")
            if bool(row["demand_admitted"]):
                raise StoreConflict(
                    f"obligation {duty} already has an admitted demand; "
                    "a second consumer registers its own DemandRef"
                )
            connection.execute(
                "UPDATE obligations SET demand_admitted = 1, updated_at = ?"
                " WHERE mission_id = ? AND obligation_id = ?",
                (self._store.now, mission, duty),
            )
            return self.account(mission, target)

    def withdraw_demand(self, mission_id: str, target: ObligationId) -> ObligationAccountView:
        """Release the admitted demand.  This ends a share, never the duty itself."""

        mission = identifier(mission_id, "mission_id")
        duty = str(_obligation_id(target, "obligation_id"))
        with self._store.transaction() as connection:
            row = self._require_row(connection, mission, duty)
            if not bool(row["demand_admitted"]):
                raise StoreConflict(f"obligation {duty} has no admitted demand to withdraw")
            connection.execute(
                "UPDATE obligations SET demand_admitted = 0, updated_at = ?"
                " WHERE mission_id = ? AND obligation_id = ?",
                (self._store.now, mission, duty),
            )
            return self.account(mission, target)

    # ------------------------------------------------------------------ recursion fuel
    def consume_fuel(
        self, mission_id: str, target: ObligationId, *, expansion: ExpansionRecord
    ) -> FuelDecision:
        """Spend one unit of the duty's fuel.  Exhaustion is ``BOUND_REACHED`` (ADR-08)."""

        if not isinstance(expansion, ExpansionRecord):
            raise StoreConflict("consume_fuel expects an ExpansionRecord")
        mission = identifier(mission_id, "mission_id")
        duty = str(_obligation_id(target, "obligation_id"))
        # Fuel level, repeat check and ordinal are all read inside the write
        # transaction: reading them outside would let two expansions share the last
        # unit of fuel, which is precisely the bound ADR-08 says must hold.
        with self._store.transaction() as connection:
            row = self._require_row(connection, mission, duty)
            remaining = int(row["fuel_remaining"])
            seen = connection.execute(
                "SELECT 1 FROM obligation_expansions WHERE mission_id = ? AND obligation_id = ?"
                " AND method_id = ? AND parameters_digest = ?",
                (mission, duty, expansion.method_id, expansion.parameters_digest),
            ).fetchone()
            if seen is not None:
                return FuelDecision(
                    FuelStatus.REPEATED_EXPANSION,
                    remaining,
                    "this obligation was already expanded with the same method and parameters",
                )
            if remaining <= 0:
                return FuelDecision(
                    FuelStatus.BOUND_REACHED, 0, "recursion fuel for this obligation is exhausted"
                )
            ordinal = self._next_ordinal(connection, "obligation_expansions", mission, duty)
            now = self._store.now
            try:
                connection.execute(
                    "INSERT INTO obligation_expansions(mission_id,obligation_id,method_id,"
                    "parameters_digest,ordinal,task_id,created_at) VALUES (?,?,?,?,?,?,?)",
                    (
                        mission,
                        duty,
                        expansion.method_id,
                        expansion.parameters_digest,
                        ordinal,
                        expansion.task_id,
                        now,
                    ),
                )
                connection.execute(
                    "UPDATE obligations SET fuel_used = fuel_used + 1,"
                    " fuel_remaining = fuel_remaining - 1, updated_at = ?"
                    " WHERE mission_id = ? AND obligation_id = ?",
                    (now, mission, duty),
                )
            except sqlite3.IntegrityError as error:
                raise StoreConflict(f"expansion of {duty} was rejected: {error}") from error
        return FuelDecision(FuelStatus.GRANTED, remaining - 1, "expansion admitted")

    def remaining_fuel(self, mission_id: str, target: ObligationId) -> int:
        return int(self._row(mission_id, target)["fuel_remaining"])

    def expansion_keys(self, mission_id: str, target: ObligationId) -> tuple[tuple[str, str], ...]:
        row = self._row(mission_id, target)
        rows = self._store.connection.execute(
            "SELECT method_id, parameters_digest FROM obligation_expansions"
            " WHERE mission_id = ? AND obligation_id = ? ORDER BY ordinal",
            (row["mission_id"], row["obligation_id"]),
        ).fetchall()
        return tuple((str(item[0]), str(item[1])) for item in rows)

    def expansions(self, mission_id: str, target: ObligationId) -> tuple[ExpansionRecord, ...]:
        row = self._row(mission_id, target)
        rows = self._store.connection.execute(
            "SELECT method_id, parameters_digest, task_id FROM obligation_expansions"
            " WHERE mission_id = ? AND obligation_id = ? ORDER BY ordinal",
            (row["mission_id"], row["obligation_id"]),
        ).fetchall()
        # Through the contract codec, not by re-deriving the type here: a hand-edited
        # or migrated row is rejected by the same validators the wire form uses.
        return tuple(
            ExpansionRecord.from_json(
                {
                    "method_id": item[0],
                    "parameters_digest": item[1],
                    "task_id": item[2],
                }
            )
            for item in rows
        )

    # ------------------------------------------------------------------ relations
    def add_relation(
        self,
        mission_id: str,
        *,
        parent: ObligationId,
        child: ObligationId,
        kind: ObligationRelation,
        active_revision: int = 0,
        detail: dict[str, Any] | None = None,
    ) -> ObligationRelationRow:
        mission = identifier(mission_id, "mission_id")
        parent_id = _obligation_id(parent, "parent")
        child_id = _obligation_id(child, "child")
        relation = enum_of(ObligationRelation, kind, "kind")
        revision = index(active_revision, "active_revision")
        if parent_id == child_id:
            raise StoreConflict("an obligation may not refine itself")
        payload = dict(detail or {})
        with self._store.transaction() as connection:
            try:
                connection.execute(
                    "INSERT INTO obligation_relations(mission_id,parent_obligation_id,"
                    "child_obligation_id,kind,active_revision,detail_json,created_at)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (
                        mission,
                        str(parent_id),
                        str(child_id),
                        str(relation),
                        revision,
                        canonical_json(payload),
                        self._store.now,
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise StoreConflict(f"obligation relation rejected: {error}") from error
        return ObligationRelationRow(
            mission_id=mission,
            parent_obligation_id=parent_id,
            child_obligation_id=child_id,
            kind=relation,
            active_revision=revision,
            detail=payload,
        )

    def list_relations(
        self,
        mission_id: str,
        *,
        parent: ObligationId | None = None,
        child: ObligationId | None = None,
    ) -> tuple[ObligationRelationRow, ...]:
        clauses = ["mission_id = ?"]
        values: list[Any] = [identifier(mission_id, "mission_id")]
        if parent is not None:
            clauses.append("parent_obligation_id = ?")
            values.append(str(_obligation_id(parent, "parent")))
        if child is not None:
            clauses.append("child_obligation_id = ?")
            values.append(str(_obligation_id(child, "child")))
        rows = self._store.connection.execute(
            "SELECT mission_id,parent_obligation_id,child_obligation_id,kind,active_revision,"
            f"detail_json FROM obligation_relations WHERE {' AND '.join(clauses)}"
            " ORDER BY created_at, parent_obligation_id, child_obligation_id",
            tuple(values),
        ).fetchall()
        return tuple(
            ObligationRelationRow(
                mission_id=str(row[0]),
                parent_obligation_id=ObligationId(row[1]),
                child_obligation_id=ObligationId(row[2]),
                kind=ObligationRelation(row[3]),
                active_revision=int(row[4]),
                detail=json.loads(row[5]),
            )
            for row in rows
        )

    # ------------------------------------------------------------------ ledger bridge
    def load_ledger(self, mission_id: str) -> ObligationLedger:
        """Rebuild the in-memory ledger of one Mission, counters and all.

        The replay is exact: a stored expansion was admitted when it was written,
        so replaying them in ordinal order reproduces ``fuel_used`` without ever
        handing out an allowance the duty did not have.
        """

        mission = identifier(mission_id, "mission_id")
        ledger = ObligationLedger()
        rows = self._store.connection.execute(
            "SELECT obligation_id, obligation_json, fuel_limit, failure_count, spent_cost_micros,"
            " spent_attempts, spent_tokens, demand_admitted, lifecycle, resolution_ref"
            " FROM obligations WHERE mission_id = ? ORDER BY created_at, obligation_id",
            (mission,),
        ).fetchall()
        for row in rows:
            duty = Obligation.from_json(json.loads(row["obligation_json"]))
            ledger.register(duty, recursion_fuel=int(row["fuel_limit"]))
            target = duty.obligation_id
            if int(row["failure_count"]):
                ledger.record_failure(target, count=int(row["failure_count"]))
            ledger.record_spend(
                target,
                cost_micros=int(row["spent_cost_micros"]),
                attempts=int(row["spent_attempts"]),
                tokens=int(row["spent_tokens"]),
            )
            for expansion in self.expansions(mission, target):
                ledger.consume_fuel(target, expansion=expansion)
            for change, detail in self.shape_changes(mission, target):
                ledger.note_shape_change(target, change, detail=detail)
            # Before the lifecycle: a demand may only be admitted while the duty is open.
            if bool(row["demand_admitted"]):
                ledger.admit_demand(target)
            lifecycle = ObligationLifecycle(row["lifecycle"])
            if lifecycle is not duty.lifecycle or row["resolution_ref"] != duty.resolution_ref:
                ledger.set_lifecycle(target, lifecycle, resolution_ref=row["resolution_ref"])
        return ledger

    def persist(self, ledger: ObligationLedger) -> tuple[ObligationId, ...]:
        """Write a ledger back.  Unknown duties are inserted, known ones take its counters.

        All three spend axes round-trip, ``spent_tokens`` included: the ledger now
        carries tokens, so writing back what :meth:`load_ledger` produced restores
        exactly what was read, and a caller that never touched tokens cannot zero them.
        """

        if not isinstance(ledger, ObligationLedger):
            raise StoreConflict("persist expects an ObligationLedger")
        written: list[ObligationId] = []
        with self._store.transaction() as connection:
            for target in ledger.obligation_ids():
                duty = ledger.obligation(target)
                view = ledger.account(target)
                now = self._store.now
                connection.execute(
                    "INSERT INTO obligations(mission_id,obligation_id,goal_signature_id,scope,"
                    "requiredness,lifecycle,resolution_ref,parent_obligation_id,"
                    "budget_lineage_ref,failure_count,spent_tokens,spent_cost_micros,"
                    "spent_attempts,fuel_limit,fuel_used,fuel_remaining,demand_admitted,"
                    "obligation_json,created_at,updated_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
                    " ON CONFLICT(mission_id,obligation_id) DO UPDATE SET"
                    " lifecycle = excluded.lifecycle, resolution_ref = excluded.resolution_ref,"
                    " failure_count = excluded.failure_count,"
                    " spent_tokens = excluded.spent_tokens,"
                    " spent_cost_micros = excluded.spent_cost_micros,"
                    " spent_attempts = excluded.spent_attempts,"
                    " fuel_limit = excluded.fuel_limit, fuel_used = excluded.fuel_used,"
                    " fuel_remaining = excluded.fuel_remaining,"
                    " demand_admitted = excluded.demand_admitted,"
                    " obligation_json = excluded.obligation_json, updated_at = excluded.updated_at",
                    (
                        duty.mission_id,
                        str(target),
                        duty.goal_signature_id,
                        duty.scope,
                        str(duty.requiredness),
                        str(view.lifecycle),
                        view.resolution_ref,
                        (
                            None
                            if duty.parent_obligation_id is None
                            else str(duty.parent_obligation_id)
                        ),
                        duty.budget_lineage_ref,
                        view.failure_count,
                        view.consumed_tokens,
                        view.consumed_cost_micros,
                        view.consumed_attempts,
                        view.fuel_limit,
                        view.fuel_used,
                        view.remaining_fuel,
                        1 if view.has_admitted_demand else 0,
                        canonical_json(
                            {
                                **duty.to_json(),
                                "lifecycle": str(view.lifecycle),
                                "resolution_ref": view.resolution_ref,
                            }
                        ),
                        now,
                        now,
                    ),
                )
                for ordinal, key in enumerate(ledger.expansion_keys(target), start=1):
                    connection.execute(
                        "INSERT INTO obligation_expansions(mission_id,obligation_id,method_id,"
                        "parameters_digest,ordinal,task_id,created_at) VALUES (?,?,?,?,?,NULL,?)"
                        " ON CONFLICT DO NOTHING",
                        (duty.mission_id, str(target), key[0], key[1], ordinal, now),
                    )
                connection.execute(
                    "DELETE FROM obligation_shape_changes WHERE mission_id = ?"
                    " AND obligation_id = ?",
                    (duty.mission_id, str(target)),
                )
                for ordinal, (change, detail) in enumerate(ledger.shape_changes(target), start=1):
                    connection.execute(
                        "INSERT INTO obligation_shape_changes(mission_id,obligation_id,ordinal,"
                        "change,detail,created_at) VALUES (?,?,?,?,?,?)",
                        (duty.mission_id, str(target), ordinal, str(change), detail, now),
                    )
                written.append(target)
        return tuple(written)

    # ------------------------------------------------------------------ internals
    @staticmethod
    def _require_row(connection: sqlite3.Connection, mission_id: str, duty: str) -> sqlite3.Row:
        """Read the row *inside* the caller's transaction, so it cannot go stale."""

        row = connection.execute(
            "SELECT * FROM obligations WHERE mission_id = ? AND obligation_id = ?",
            (mission_id, duty),
        ).fetchone()
        if row is None:
            raise StoreConflict(f"obligation {duty} is not registered in mission {mission_id}")
        return row

    @staticmethod
    def _next_ordinal(
        connection: sqlite3.Connection, table: str, mission_id: str, duty: str
    ) -> int:
        row = connection.execute(
            f"SELECT coalesce(max(ordinal), 0) FROM {table}"  # noqa: S608
            " WHERE mission_id = ? AND obligation_id = ?",
            (mission_id, duty),
        ).fetchone()
        return int(row[0]) + 1

    def _row(self, mission_id: str, target: ObligationId) -> sqlite3.Row:
        mission = identifier(mission_id, "mission_id")
        duty = _obligation_id(target, "obligation_id")
        row = self._store.connection.execute(
            "SELECT * FROM obligations WHERE mission_id = ? AND obligation_id = ?",
            (mission, str(duty)),
        ).fetchone()
        if row is None:
            raise StoreConflict(f"obligation {duty!s} is not registered in mission {mission}")
        return row

    def _count(self, table: str, mission_id: str, target: str) -> int:
        row = self._store.connection.execute(
            f"SELECT count(*) FROM {table} WHERE mission_id = ? AND obligation_id = ?",  # noqa: S608
            (mission_id, target),
        ).fetchone()
        return int(row[0])


__all__ = ("DEFAULT_RECURSION_FUEL", "ObligationRelationRow", "ObligationStore")
