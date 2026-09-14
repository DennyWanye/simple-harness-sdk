# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""Durable same-host capacity shared by independent SDK dispatchers."""

from __future__ import annotations

import errno
import importlib
import os
import secrets
import sqlite3
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

_ACTIVE = ("WAITING", "RESERVED", "HANDED_OFF", "UNKNOWN")
_HELD = ("RESERVED", "HANDED_OFF", "UNKNOWN")


class CapacityLedgerError(RuntimeError):
    """Base error for durable capacity coordination."""


class CapacityConfigurationError(CapacityLedgerError):
    """The physical pool was reopened with different fixed limits."""


class CapacityKeyError(CapacityLedgerError):
    """A durable key cannot be reused by this enqueue operation."""


class InvalidCapacityTicket(CapacityLedgerError):
    """The ticket capability does not identify its original owner epoch."""


class CapacityTransitionError(CapacityLedgerError):
    """The requested lifecycle transition is unsafe for the current state."""


@dataclass(frozen=True, slots=True)
class CapacityTicket:
    pool_id: str
    key: str
    owner: str
    epoch: str
    weight: int


@dataclass(frozen=True, slots=True)
class CapacityRow:
    sequence: int
    key: str
    owner: str
    pid: int
    weight: int
    state: str
    evidence_ref: str | None


@dataclass(frozen=True, slots=True)
class CapacitySnapshot:
    pool_id: str
    max_slots: int
    max_tokens: int
    held_slots: int
    held_tokens: int
    rows: tuple[CapacityRow, ...]


def _text(name: str, value: object) -> str:
    if type(value) is not str or not value:  # strict: subclasses and empty IDs are invalid
        raise TypeError(f"{name} must be a non-empty str")
    return value


def _positive_int(name: str, value: object) -> int:
    if type(value) is not int or value <= 0:
        raise TypeError(f"{name} must be a positive int")
    return value


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError as error:
        return error.errno != errno.ESRCH
    return True  # includes conservative PID-reuse handling


def _process_identity(pid: int) -> str | None:
    try:
        psutil = importlib.import_module("psutil")
    except ImportError as error:
        raise RuntimeError("shared capacity requires the SDK local-capacity extra") from error
    if tuple(psutil.version_info) < (7, 2, 2):
        raise RuntimeError("shared capacity requires psutil >= 7.2.2")
    if sys.platform not in {"darwin", "linux", "win32"}:
        return None  # no stable identity attested on other platforms
    try:
        # psutil's public process hash combines PID with its stable process
        # identity (monotonic kernel creation time on macOS/Linux). In contrast,
        # create_time() is wall-clock adjusted and can differ across processes
        # after NTP updates. A hash collision only retains capacity conservatively.
        return f"psutil-process-v1:{hash(psutil.Process(pid))}"
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None  # lack of process identity never proves an active PID dead


class CapacityLedger:
    """SQLite FIFO ledger for one physical deployment pool."""

    def __init__(self, path: Path, *, pool_id: str, max_slots: int, max_tokens: int) -> None:
        if not isinstance(path, Path):
            raise TypeError("path must be a pathlib.Path")
        self.path = path
        self.pool_id = _text("pool_id", pool_id)
        self.max_slots = _positive_int("max_slots", max_slots)
        self.max_tokens = _positive_int("max_tokens", max_tokens)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, isolation_level=None, timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._recover_dead(connection)
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        connection = self._connect()
        limits = (self.pool_id, self.max_slots, self.max_tokens)
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """CREATE TABLE IF NOT EXISTS shared_capacity_pools (
                    pool_id TEXT PRIMARY KEY,
                    max_slots INTEGER NOT NULL CHECK(max_slots > 0),
                    max_tokens INTEGER NOT NULL CHECK(max_tokens > 0)
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS shared_capacity_entries (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    pool_id TEXT NOT NULL,
                    key TEXT NOT NULL,
                    owner TEXT NOT NULL,
                    epoch TEXT NOT NULL,
                    pid INTEGER NOT NULL CHECK(pid > 0),
                    weight INTEGER NOT NULL CHECK(weight > 0),
                    state TEXT NOT NULL CHECK(state IN
                        ('WAITING','RESERVED','HANDED_OFF','UNKNOWN','RELEASED','SETTLED')),
                    evidence_ref TEXT CHECK(evidence_ref IS NULL OR length(evidence_ref) > 0),
                    UNIQUE(pool_id, key),
                    UNIQUE(pool_id, epoch),
                    FOREIGN KEY(pool_id) REFERENCES shared_capacity_pools(pool_id)
                )"""
            )
            columns = {
                item["name"]
                for item in connection.execute("PRAGMA table_info(shared_capacity_entries)")
            }
            if "evidence_ref" not in columns:
                connection.execute(
                    "ALTER TABLE shared_capacity_entries ADD COLUMN evidence_ref TEXT "
                    "CHECK(evidence_ref IS NULL OR length(evidence_ref) > 0)"
                )
            if "process_identity" not in columns:
                connection.execute(
                    "ALTER TABLE shared_capacity_entries ADD COLUMN process_identity TEXT"
                )
            row = connection.execute(
                "SELECT max_slots, max_tokens FROM shared_capacity_pools WHERE pool_id = ?",
                (self.pool_id,),
            ).fetchone()
            if row is None:
                connection.execute("INSERT INTO shared_capacity_pools VALUES (?, ?, ?)", limits)
            elif (row["max_slots"], row["max_tokens"]) != limits[1:]:
                raise CapacityConfigurationError(
                    f"pool {self.pool_id!r} already has different fixed limits"
                )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _recover_dead(self, connection: sqlite3.Connection) -> None:
        rows = connection.execute(
            """SELECT sequence, pid, state, process_identity FROM shared_capacity_entries
               WHERE pool_id = ? AND state IN ('WAITING','RESERVED','HANDED_OFF')""",
            (self.pool_id,),
        ).fetchall()
        for row in rows:
            if _pid_alive(row["pid"]):
                current = _process_identity(row["pid"])
                if (
                    row["process_identity"] is None
                    or current is None
                    or current == row["process_identity"]
                ):
                    continue
            state = "UNKNOWN" if row["state"] == "HANDED_OFF" else "RELEASED"
            connection.execute(
                "UPDATE shared_capacity_entries SET state = ? WHERE sequence = ? AND state = ?",
                (state, row["sequence"], row["state"]),
            )

    def _ticket_row(self, connection: sqlite3.Connection, ticket: CapacityTicket) -> sqlite3.Row:
        if type(ticket) is not CapacityTicket:
            raise TypeError("ticket must be a CapacityTicket")
        row = connection.execute(
            """SELECT * FROM shared_capacity_entries
               WHERE pool_id = ? AND key = ? AND owner = ? AND epoch = ? AND weight = ?""",
            (ticket.pool_id, ticket.key, ticket.owner, ticket.epoch, ticket.weight),
        ).fetchone()
        if ticket.pool_id != self.pool_id or row is None:
            raise InvalidCapacityTicket("ticket does not match its durable owner epoch")
        return row

    def enqueue(self, key: str, *, weight: int, owner: str, pid: int) -> CapacityTicket:
        key = _text("key", key)
        owner = _text("owner", owner)
        weight = _positive_int("weight", weight)
        pid = _positive_int("pid", pid)
        if weight > self.max_tokens:
            raise ValueError("weight exceeds pool token capacity")
        with self._write() as connection:
            row = connection.execute(
                "SELECT owner, epoch, weight, state FROM shared_capacity_entries "
                "WHERE pool_id = ? AND key = ?",
                (self.pool_id, key),
            ).fetchone()
            if row is not None:
                if row["state"] in _ACTIVE and row["owner"] == owner and row["weight"] == weight:
                    return CapacityTicket(self.pool_id, key, owner, row["epoch"], weight)
                raise CapacityKeyError("key is terminal or belongs to another owner/weight")
            epoch = secrets.token_urlsafe(24)
            connection.execute(
                """INSERT INTO shared_capacity_entries
                   (pool_id, key, owner, epoch, pid, weight, state)
                   VALUES (?, ?, ?, ?, ?, ?, 'WAITING')""",
                (self.pool_id, key, owner, epoch, pid, weight),
            )
            connection.execute(
                "UPDATE shared_capacity_entries SET process_identity=? WHERE pool_id=? AND key=?",
                (_process_identity(pid), self.pool_id, key),
            )
            return CapacityTicket(self.pool_id, key, owner, epoch, weight)

    def try_acquire(self, ticket: CapacityTicket) -> bool:
        with self._write() as connection:
            row = self._ticket_row(connection, ticket)
            if row["state"] == "RESERVED":
                return True
            if row["state"] != "WAITING":
                raise CapacityTransitionError(f"cannot acquire from {row['state']}")
            head = connection.execute(
                """SELECT sequence FROM shared_capacity_entries
                   WHERE pool_id = ? AND state = 'WAITING' ORDER BY sequence LIMIT 1""",
                (self.pool_id,),
            ).fetchone()
            held = connection.execute(
                """SELECT COUNT(*) AS slots, COALESCE(SUM(weight), 0) AS tokens
                   FROM shared_capacity_entries WHERE pool_id = ?
                   AND state IN ('RESERVED','HANDED_OFF','UNKNOWN')""",
                (self.pool_id,),
            ).fetchone()
            if (
                head["sequence"] != row["sequence"]
                or held["slots"] >= self.max_slots
                or held["tokens"] + row["weight"] > self.max_tokens
            ):
                return False
            connection.execute(
                """UPDATE shared_capacity_entries SET state = 'RESERVED'
                   WHERE sequence = ? AND owner = ? AND epoch = ? AND state = 'WAITING'""",
                (row["sequence"], ticket.owner, ticket.epoch),
            )
            return True

    def mark_handed_off(self, ticket: CapacityTicket) -> None:
        self._transition(ticket, "RESERVED", "HANDED_OFF")

    def finish(self, ticket: CapacityTicket, *, known_terminal: bool) -> None:
        if type(known_terminal) is not bool:
            raise TypeError("known_terminal must be a bool")
        with self._write() as connection:
            row = self._ticket_row(connection, ticket)
            if row["state"] == "RESERVED":
                target = "RELEASED"
            elif row["state"] in ("HANDED_OFF", "UNKNOWN"):
                target = "SETTLED" if known_terminal else "UNKNOWN"
            elif row["state"] in ("RELEASED", "SETTLED"):
                return
            else:
                raise CapacityTransitionError(f"cannot finish from {row['state']}")
            connection.execute(
                "UPDATE shared_capacity_entries SET state = ? "
                "WHERE sequence = ? AND owner = ? AND epoch = ? AND state = ?",
                (target, row["sequence"], ticket.owner, ticket.epoch, row["state"]),
            )

    def cancel_waiter(self, ticket: CapacityTicket) -> None:
        with self._write() as connection:
            row = self._ticket_row(connection, ticket)
            if row["state"] not in ("WAITING", "RESERVED"):
                raise CapacityTransitionError(f"cannot cancel from {row['state']}")
            connection.execute(
                """UPDATE shared_capacity_entries SET state = 'RELEASED'
                   WHERE sequence = ? AND owner = ? AND epoch = ? AND state = ?""",
                (row["sequence"], ticket.owner, ticket.epoch, row["state"]),
            )

    def reconcile(self, key: str, *, evidence_ref: str) -> None:
        """Settle an uncertain physical handoff from trusted SDK evidence.
        Caller must verify same-invocation+ordinal terminal usage or
        ``CONFIRMED_NOT_STARTED``; TTL/PID inference is forbidden. The opaque
        identity/version ref contains no response/credential. This is not a model
        tool, and the ledger does not interpret SDK proof.
        """
        key = _text("key", key)
        evidence_ref = _text("evidence_ref", evidence_ref)
        with self._write() as connection:
            row = connection.execute(
                "SELECT sequence, state, evidence_ref FROM shared_capacity_entries "
                "WHERE pool_id = ? AND key = ?",
                (self.pool_id, key),
            ).fetchone()
            if row is None:
                raise CapacityKeyError("unknown reconciliation key")
            if row["state"] == "SETTLED":
                if row["evidence_ref"] == evidence_ref:
                    return
                raise CapacityTransitionError("settled key has different reconciliation proof")
            if row["state"] not in ("HANDED_OFF", "UNKNOWN"):
                raise CapacityTransitionError(f"cannot reconcile from {row['state']}")
            connection.execute(
                "UPDATE shared_capacity_entries SET state = 'SETTLED', evidence_ref = ? "
                "WHERE sequence = ? AND state IN ('HANDED_OFF','UNKNOWN')",
                (evidence_ref, row["sequence"]),
            )

    def _transition(self, ticket: CapacityTicket, source: str, target: str) -> None:
        with self._write() as connection:
            row = self._ticket_row(connection, ticket)
            if row["state"] == target:
                return
            if row["state"] != source:
                raise CapacityTransitionError(f"cannot transition from {row['state']} to {target}")
            connection.execute(
                "UPDATE shared_capacity_entries SET state = ? "
                "WHERE sequence = ? AND owner = ? AND epoch = ? AND state = ?",
                (target, row["sequence"], ticket.owner, ticket.epoch, source),
            )

    def snapshot(self) -> CapacitySnapshot:
        with self._write() as connection:
            rows = connection.execute(
                """SELECT sequence, key, owner, pid, weight, state, evidence_ref
                   FROM shared_capacity_entries WHERE pool_id = ? ORDER BY sequence""",
                (self.pool_id,),
            ).fetchall()
        audit_rows = tuple(CapacityRow(*row) for row in rows)
        held = tuple(row for row in audit_rows if row.state in _HELD)
        return CapacitySnapshot(
            self.pool_id,
            self.max_slots,
            self.max_tokens,
            len(held),
            sum(row.weight for row in held),
            audit_rows,
        )
