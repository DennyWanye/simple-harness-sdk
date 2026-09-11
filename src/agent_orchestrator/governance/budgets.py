# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501  (SQL statement literals)

"""Budget accounts, Reserve / Settle and usage import (§18, ORCH-BUILD §12.2).

Two layers, never mixed:

* **Allocation layer** (this module): ``budget_accounts`` for Mission → Task →
  Attempt, ``budget_reservations`` per dispatch subject.  Reserve before an
  Attempt starts (§18.3), Settle with the real cost afterwards, release what was
  not used.  Child limits never exceed the parent (§18.2) and a child allocation
  never *adds* funds.
* **Fact layer** (the SDK ``provider_invocations`` ledger): the only source of
  actual token / money figures.  ``import_usage`` copies each ``usage_ref`` at
  most once (``imported_usage`` PK).  In ``unpriced`` mode the money column stays
  ``NULL`` and the settlement is flagged ``unpriced`` — never written as zero.

An UNKNOWN provider call keeps its reservation occupied: ``settle`` refuses to
run until the caller says every usage ref is final.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from simple_harness.contracts import canonical_json

from ..contracts import Budget
from ..storage.store import Store, StoreError


class BudgetError(StoreError):
    pass


class BudgetExhausted(BudgetError):
    """Reserve refused: the requested allocation does not fit the remaining budget."""

    def __init__(self, account_id: str, dimension: str, requested: int, remaining: int) -> None:
        super().__init__(
            f"budget exhausted on {account_id}: {dimension} requested {requested}, remaining {remaining}"
        )
        self.account_id = account_id
        self.dimension = dimension
        self.requested = requested
        self.remaining = remaining


@dataclass(frozen=True, slots=True)
class UsageFact:
    """One SDK provider invocation as imported from the fact layer."""

    usage_ref: str
    input_tokens: int
    output_tokens: int
    cost_micros: int | None  # None == unpriced (deployment has no price table)
    unknown: bool = False  # priced deployment, but the SDK could not price this call

    @property
    def tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    account_id: str
    scope: str
    parent_id: str | None
    limits: Budget
    reserved_tokens: int
    settled_tokens: int
    reserved_cost_micros: int
    settled_cost_micros: int
    unpriced_settlements: int
    attempts_created: int
    version: int
    reserved_tool_calls: int = 0
    settled_tool_calls: int = 0

    def remaining_tool_calls(self) -> int | None:
        if self.limits.max_tool_calls is None:
            return None
        return self.limits.max_tool_calls - self.reserved_tool_calls - self.settled_tool_calls

    def remaining_tokens(self) -> int | None:
        if self.limits.max_tokens is None:
            return None
        return self.limits.max_tokens - self.reserved_tokens - self.settled_tokens

    def remaining_cost_micros(self) -> int | None:
        if self.limits.max_cost_micros is None:
            return None
        return self.limits.max_cost_micros - self.reserved_cost_micros - self.settled_cost_micros

    def remaining_attempts(self) -> int | None:
        if self.limits.max_attempts is None:
            return None
        return self.limits.max_attempts - self.attempts_created

    def to_json(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "scope": self.scope,
            "parent_id": self.parent_id,
            "limits": self.limits.to_json(),
            "reserved_tokens": self.reserved_tokens,
            "settled_tokens": self.settled_tokens,
            "reserved_cost_micros": self.reserved_cost_micros,
            "settled_cost_micros": self.settled_cost_micros,
            "unpriced_settlements": self.unpriced_settlements,
            "attempts_created": self.attempts_created,
            "reserved_tool_calls": self.reserved_tool_calls,
            "settled_tool_calls": self.settled_tool_calls,
            "remaining_tool_calls": self.remaining_tool_calls(),
            "remaining_tokens": self.remaining_tokens(),
            "remaining_cost_micros": self.remaining_cost_micros(),
            "remaining_attempts": self.remaining_attempts(),
            "version": self.version,
        }


class BudgetLedger:
    """All methods must run inside a ``Store.transaction()`` opened by the Commit Service."""

    def __init__(self, store: Store) -> None:
        self._store = store

    # ---------------------------------------------------------------- accounts
    def open_account(
        self, *, account_id: str, scope: str, parent_id: str | None, mission_id: str, limits: Budget
    ) -> AccountSnapshot:
        if parent_id is not None:
            parent = self.account(parent_id)
            if not limits.fits_within(parent.limits):
                raise BudgetError(
                    f"{scope} budget {limits.to_json()} exceeds parent {parent.limits.to_json()} (§18.2)"
                )
        self._store.connection.execute(
            "INSERT INTO budget_accounts(account_id,scope,parent_id,mission_id,limits_json,version,updated_at)"
            " VALUES (?,?,?,?,?,1,?) ON CONFLICT(account_id) DO NOTHING",
            (
                account_id,
                scope,
                parent_id,
                mission_id,
                canonical_json(limits.to_json()),
                self._store.now,
            ),
        )
        return self.account(account_id)

    def account(self, account_id: str) -> AccountSnapshot:
        row = self._store.connection.execute(
            "SELECT * FROM budget_accounts WHERE account_id = ?", (account_id,)
        ).fetchone()
        if row is None:
            raise BudgetError(f"unknown budget account {account_id}")
        import json

        return AccountSnapshot(
            account_id=row["account_id"],
            scope=row["scope"],
            parent_id=row["parent_id"],
            limits=Budget.from_json(json.loads(row["limits_json"])),
            reserved_tokens=row["reserved_tokens"],
            settled_tokens=row["settled_tokens"],
            reserved_cost_micros=row["reserved_cost_micros"],
            settled_cost_micros=row["settled_cost_micros"],
            unpriced_settlements=row["unpriced_settlements"],
            attempts_created=row["attempts_created"],
            version=row["version"],
            reserved_tool_calls=int(row["reserved_tool_calls"] or 0),
            settled_tool_calls=int(row["settled_tool_calls"] or 0),
        )

    def _chain(self, account_id: str) -> list[AccountSnapshot]:
        chain = []
        current: str | None = account_id
        while current is not None:
            snapshot = self.account(current)
            chain.append(snapshot)
            current = snapshot.parent_id
        return chain

    def _apply(self, account_id: str, **deltas: int) -> None:
        assignments = ", ".join(f"{column} = {column} + ?" for column in deltas)
        cursor = self._store.connection.execute(
            f"UPDATE budget_accounts SET {assignments}, version = version + 1, updated_at = ?"
            " WHERE account_id = ?",
            (*deltas.values(), self._store.now, account_id),
        )
        if cursor.rowcount != 1:
            raise BudgetError(f"unknown budget account {account_id}")

    # ---------------------------------------------------------------- reserve
    def reserve(
        self,
        *,
        account_id: str,
        subject_id: str,
        tokens: int,
        cost_micros: int,
        counts_attempt: bool,
        tool_calls: int = 0,
        mission_id: str | None = None,
    ) -> str:
        """Reserve ``tokens`` / ``cost_micros`` (/ ``tool_calls``) on ``account_id`` and every ancestor.

        Idempotent per ``subject_id``: a second call returns the existing reservation.
        Fails closed on the first dimension that does not fit (§18.3: 避免并发 Agent 同时超支).
        """

        existing = self._store.connection.execute(
            "SELECT reservation_id FROM budget_reservations WHERE subject_id = ?", (subject_id,)
        ).fetchone()
        if existing is not None:
            return str(existing[0])
        chain = self._chain(account_id)
        for snapshot in chain:
            remaining_tokens = snapshot.remaining_tokens()
            if remaining_tokens is not None and tokens > remaining_tokens:
                raise BudgetExhausted(snapshot.account_id, "tokens", tokens, remaining_tokens)
            remaining_cost = snapshot.remaining_cost_micros()
            if remaining_cost is not None and cost_micros > remaining_cost:
                raise BudgetExhausted(
                    snapshot.account_id, "cost_micros", cost_micros, remaining_cost
                )
            if counts_attempt:
                remaining_attempts = snapshot.remaining_attempts()
                if remaining_attempts is not None and remaining_attempts < 1:
                    raise BudgetExhausted(snapshot.account_id, "attempts", 1, remaining_attempts)
            remaining_calls = snapshot.remaining_tool_calls()
            if remaining_calls is not None and tool_calls > remaining_calls:
                raise BudgetExhausted(
                    snapshot.account_id, "tool_calls", tool_calls, remaining_calls
                )
        for snapshot in chain:
            self._apply(
                snapshot.account_id,
                reserved_tokens=tokens,
                reserved_cost_micros=cost_micros,
                reserved_tool_calls=tool_calls,
                attempts_created=1 if counts_attempt else 0,
            )
        reservation_id = f"reservation-{subject_id}"
        if (
            mission_id is None
        ):  # review P0-1: the caller names the Mission; the chain may end at Global
            mission_id = next(
                (s.account_id.removeprefix("budget:") for s in chain if s.scope == "mission"),
                chain[-1].account_id.removeprefix("budget:"),
            )
        self._store.connection.execute(
            "INSERT INTO budget_reservations(reservation_id,account_id,mission_id,subject_id,state,"
            "reserved_tokens,reserved_cost_micros,reserved_tool_calls,created_at,updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                reservation_id,
                account_id,
                mission_id,
                subject_id,
                "RESERVED",
                tokens,
                cost_micros,
                tool_calls,
                self._store.now,
                self._store.now,
            ),
        )
        return reservation_id

    def reservation(self, subject_id: str) -> dict[str, Any] | None:
        row = self._store.connection.execute(
            "SELECT * FROM budget_reservations WHERE subject_id = ?", (subject_id,)
        ).fetchone()
        return None if row is None else dict(row)

    # ------------------------------------------------------------ usage facts
    def import_usage(self, *, subject_id: str, mission_id: str, facts: Sequence[UsageFact]) -> int:
        """Copy usage facts from the SDK ledger; each ``usage_ref`` lands at most once."""

        imported = 0
        for fact in facts:
            cursor = self._store.connection.execute(
                "INSERT INTO imported_usage(usage_ref,subject_id,mission_id,input_tokens,output_tokens,"
                "cost_micros,unpriced,unknown,imported_at) VALUES (?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(usage_ref) DO NOTHING",
                (
                    fact.usage_ref,
                    subject_id,
                    mission_id,
                    fact.input_tokens,
                    fact.output_tokens,
                    fact.cost_micros,
                    0 if (fact.cost_micros is not None or fact.unknown) else 1,
                    1 if fact.unknown else 0,
                    self._store.now,
                ),
            )
            imported += cursor.rowcount
        return imported

    def usage_for(self, subject_id: str) -> tuple[int, int | None, bool]:
        """(tokens, cost_micros or None, unpriced_present) over the subject's imported usage."""

        row = self._store.connection.execute(
            "SELECT COALESCE(SUM(input_tokens + output_tokens), 0), SUM(cost_micros), MAX(unpriced)"
            " FROM imported_usage WHERE subject_id = ?",
            (subject_id,),
        ).fetchone()
        tokens = int(row[0])
        unpriced = bool(row[2])
        cost = None if unpriced or row[1] is None else int(row[1])
        return tokens, cost, unpriced

    def has_unknown_usage(self, subject_id: str) -> bool:
        row = self._store.connection.execute(
            "SELECT COUNT(*) FROM imported_usage WHERE subject_id = ? AND unknown = 1",
            (subject_id,),
        ).fetchone()
        return int(row[0]) > 0

    # ----------------------------------------------------------------- settle
    def settle(self, *, subject_id: str, tool_calls: int = 0) -> dict[str, Any]:
        """Replace the reservation by the imported facts on the whole account chain
        (``tool_calls`` is the gateway's count for the subject — a fact, never a guess)."""

        reservation = self.reservation(subject_id)
        if reservation is None:
            raise BudgetError(f"no reservation for {subject_id}")
        if reservation["state"] == "SETTLED":
            return reservation
        if self.has_unknown_usage(subject_id):
            # ORCH §12.2: an UNKNOWN charge keeps the reservation occupied until reconciled.
            raise BudgetError(f"{subject_id} has an unknown provider charge; reservation held")
        tokens, cost, unpriced = self.usage_for(subject_id)
        settled_cost = 0 if cost is None else cost
        for snapshot in self._chain(reservation["account_id"]):
            self._apply(
                snapshot.account_id,
                reserved_tokens=-int(reservation["reserved_tokens"]),
                reserved_cost_micros=-int(reservation["reserved_cost_micros"]),
                reserved_tool_calls=-int(reservation.get("reserved_tool_calls") or 0),
                settled_tokens=tokens,
                settled_cost_micros=settled_cost,
                settled_tool_calls=int(tool_calls),
                unpriced_settlements=1 if unpriced else 0,
            )
        self._store.connection.execute(
            "UPDATE budget_reservations SET state = 'SETTLED', settled_tokens = ?, settled_cost_micros = ?,"
            " settled_tool_calls = ?, unpriced = ?, updated_at = ? WHERE subject_id = ?",
            (tokens, cost, int(tool_calls), 1 if unpriced else 0, self._store.now, subject_id),
        )
        settled = self.reservation(subject_id)
        assert settled is not None
        return settled

    def costs_report(self, mission_id: str) -> dict[str, Any]:
        rows = self._store.connection.execute(
            "SELECT * FROM budget_accounts WHERE mission_id = ? ORDER BY account_id", (mission_id,)
        ).fetchall()
        usage = self._store.connection.execute(
            "SELECT subject_id, usage_ref, input_tokens, output_tokens, cost_micros, unpriced, unknown"
            " FROM imported_usage WHERE mission_id = ? ORDER BY imported_at, usage_ref",
            (mission_id,),
        ).fetchall()
        reservations = self._store.connection.execute(
            "SELECT * FROM budget_reservations WHERE mission_id = ? ORDER BY created_at",
            (mission_id,),
        ).fetchall()
        global_row = self._store.connection.execute(
            "SELECT account_id FROM budget_accounts WHERE scope = 'global'"
        ).fetchone()
        return {
            "accounts": [self.account(row["account_id"]).to_json() for row in rows],
            "usage": [dict(row) for row in usage],
            "reservations": [dict(row) for row in reservations],
            "global": None if global_row is None else self.account(global_row[0]).to_json(),
        }


def budget_from_mapping(value: Mapping[str, Any] | Budget | None) -> Budget:
    if value is None:
        return Budget()
    if isinstance(value, Budget):
        return value
    return Budget.from_json(value)


__all__ = (
    "AccountSnapshot",
    "BudgetError",
    "BudgetExhausted",
    "BudgetLedger",
    "UsageFact",
    "budget_from_mapping",
)
