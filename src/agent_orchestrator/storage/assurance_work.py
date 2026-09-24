# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""Durable Assurance inbox on the original Store connection (not an executor)."""

from __future__ import annotations

import logging

import sqlite3
import uuid
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TypeVar

from ..assurance.codec import AssuranceError, digest, integer, one_of, text
from ..contracts import Event
from .store import Store, StoreConflict

CONSUMERS = frozenset({"REVIEW", "VALIDITY", "CLOSEOUT", "NOTIFY"})
T = TypeVar("T")


@contextmanager
def atomic(store: Store) -> Iterator[sqlite3.Connection]:
    """Rollback this operation even if an outer caller catches a nested failure.

    Store.transaction joins an existing transaction without a savepoint. The
    local savepoint prevents a caught WORK_TARGET_CONFLICT leaving half a page.
    """
    with store.transaction() as connection:
        name = "assurance_" + uuid.uuid4().hex
        connection.execute(f"SAVEPOINT {name}")
        try:
            yield connection
        except BaseException:
            connection.execute(f"ROLLBACK TO {name}")
            connection.execute(f"RELEASE {name}")
            raise
        else:
            connection.execute(f"RELEASE {name}")


@dataclass(frozen=True, slots=True)
class WorkTarget:
    work_key: str
    fingerprint: str

    def __post_init__(self) -> None:
        text(self.work_key)
        digest(self.fingerprint)


@dataclass(frozen=True, slots=True)
class WorkClaim:
    mission_id: str
    consumer: str
    work_key: str
    trigger_event_id: str
    target_epoch: int  # The original Event.seq, never a truth/authority epoch.
    target_fingerprint: str
    row_version: int
    tries: int
    owner: str
    lease_until_ms: int


class AssuranceWorkStore:
    def __init__(self, store: Store) -> None:
        self.store = store

    @staticmethod
    def _cursor(connection: sqlite3.Connection, mission_id: str, consumer: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM assurance_event_cursors WHERE mission_id=? AND consumer=?",
            (mission_id, consumer),
        ).fetchone()
        if row is None:
            raise AssuranceError("CURSOR_UNINITIALIZED")
        return row

    def initialize_cursor(
        self, mission_id: str, consumer: str, *, activation_seq: int, now_ms: int
    ) -> None:
        text(mission_id)
        one_of(consumer, CONSUMERS)
        integer(activation_seq)
        integer(now_ms)
        with atomic(self.store) as connection:
            old = connection.execute(
                "SELECT 1 FROM assurance_event_cursors WHERE mission_id=? AND consumer=?",
                (mission_id, consumer),
            ).fetchone()
            if old is not None:
                raise AssuranceError("CURSOR_ALREADY_INITIALIZED")
            if (
                activation_seq
                and connection.execute(
                    "SELECT 1 FROM events WHERE mission_id=? AND seq=?",
                    (mission_id, activation_seq),
                ).fetchone()
                is None
            ):
                raise AssuranceError("ACTIVATION_EVENT_MISSING")
            connection.execute(
                "INSERT INTO assurance_event_cursors VALUES(?,?,?,1,?)",
                (mission_id, consumer, activation_seq, now_ms),
            )

    def _enqueue(
        self,
        connection: sqlite3.Connection,
        mission_id: str,
        consumer: str,
        event: Event,
        target: WorkTarget,
        now_ms: int,
    ) -> bool:
        integer(event.seq, minimum=1)
        if event.mission_id != mission_id:
            raise AssuranceError("WORK_EVENT_MISSION_MISMATCH")
        stored = connection.execute(
            "SELECT mission_id,seq FROM events WHERE event_id=?", (event.id,)
        ).fetchone()
        if stored is None or stored["seq"] != event.seq or stored["mission_id"] != mission_id:
            raise AssuranceError("WORK_EVENT_NOT_DURABLE")
        identity = (mission_id, consumer, target.work_key)
        row = connection.execute(
            "SELECT * FROM assurance_pending_work WHERE mission_id=? AND consumer=? AND work_key=?",
            identity,
        ).fetchone()
        if row is None:
            connection.execute(
                "INSERT INTO assurance_pending_work(mission_id,consumer,work_key,trigger_event_id,"
                "target_epoch,target_fingerprint,state,row_version,tries,not_before_ms) "
                "VALUES(?,?,?,?,?,?,'PENDING',1,0,?)",
                (*identity, event.id, event.seq, target.fingerprint, now_ms),
            )
            return True
        if event.seq == row["target_epoch"]:
            if target.fingerprint != row["target_fingerprint"]:
                raise AssuranceError("WORK_TARGET_CONFLICT", target.work_key)
            return False
        if event.seq < row["target_epoch"]:
            return False
        # Preserve tries and any future cumulative accounting fields. A new target
        # invalidates the old claim, including DONE/REJECTED rows, without a new job.
        connection.execute(
            "UPDATE assurance_pending_work SET trigger_event_id=?,target_epoch=?,"
            "target_fingerprint=?,state='PENDING',row_version=row_version+1,"
            "not_before_ms=?,wait_reason=NULL,owner=NULL,lease_until_ms=NULL "
            "WHERE mission_id=? AND consumer=? AND work_key=? AND row_version=?",
            (event.id, event.seq, target.fingerprint, now_ms, *identity, row["row_version"]),
        )
        return True

    def ingest(
        self,
        mission_id: str,
        consumer: str,
        *,
        expected_version: int,
        classify: Callable[[Event, str], Sequence[WorkTarget]],
        now_ms: int,
        page_size: int = 128,
    ) -> int:
        """Classify an actual Store page and move its cursor in one short transaction.

        `classify` is a trusted synchronous metadata adapter. It must not do I/O,
        model work or prepare closures. IGNORE is an empty target sequence.
        """
        one_of(consumer, CONSUMERS)
        integer(now_ms)
        integer(page_size, minimum=1, maximum=128)
        integer(expected_version, minimum=1)
        with atomic(self.store) as connection:
            cursor = self._cursor(connection, mission_id, consumer)
            if cursor["row_version"] != expected_version:
                raise StoreConflict("assurance cursor changed")
            events = self.store.list_events(
                mission_id, after_seq=cursor["last_event_seq"], limit=page_size
            )
            for event in events:
                for target in classify(event, consumer):
                    self._enqueue(connection, mission_id, consumer, event, target, now_ms)
            if not events:
                return cursor["last_event_seq"]
            seq = events[-1].seq
            assert seq is not None
            connection.execute(
                "UPDATE assurance_event_cursors SET last_event_seq=?,row_version=row_version+1,"
                "updated_at_ms=? WHERE mission_id=? AND consumer=? AND row_version=?",
                (seq, now_ms, mission_id, consumer, expected_version),
            )
            return seq

    def seed(
        self,
        mission_id: str,
        consumer: str,
        event: Event,
        targets: Sequence[WorkTarget],
        *,
        now_ms: int,
    ) -> None:
        """Activation reconciliation uses the actual activation event in factory UoW."""
        one_of(consumer, CONSUMERS)
        integer(now_ms)
        with atomic(self.store) as connection:
            for target in targets:
                self._enqueue(connection, mission_id, consumer, event, target, now_ms)

    def claim_due(
        self,
        mission_id: str,
        consumer: str,
        *,
        owner: str,
        now_ms: int,
        lease_ms: int = 30_000,
        limit: int = 8,
    ) -> tuple[WorkClaim, ...]:
        one_of(consumer, CONSUMERS)
        text(owner)
        integer(now_ms)
        integer(lease_ms, minimum=1, maximum=300_000)
        integer(limit, minimum=1, maximum=8)
        claimed = []
        with atomic(self.store) as connection:
            # Expiry reclaims coordination only. It never means a Provider or
            # Operation stopped; dispatch_intents retains that responsibility.
            expired = connection.execute(
                "SELECT work_key,row_version FROM assurance_pending_work WHERE mission_id=? "
                "AND consumer=? AND state='RUNNING' AND lease_until_ms<=? "
                "ORDER BY lease_until_ms,work_key LIMIT ?",
                (mission_id, consumer, now_ms, limit),
            ).fetchall()
            for row in expired:
                connection.execute(
                    "UPDATE assurance_pending_work SET state='PENDING',row_version=row_version+1,"
                    "owner=NULL,lease_until_ms=NULL WHERE mission_id=? AND consumer=? "
                    "AND work_key=? AND row_version=?",
                    (mission_id, consumer, row["work_key"], row["row_version"]),
                )
            rows = connection.execute(
                "SELECT * FROM assurance_pending_work WHERE mission_id=? AND consumer=? "
                "AND state IN ('PENDING','WAITING') AND not_before_ms<=? "
                "AND (wait_reason IS NULL OR wait_reason<>'MANUAL_REQUIRED') "
                "ORDER BY not_before_ms,work_key LIMIT ?",
                (mission_id, consumer, now_ms, limit),
            ).fetchall()
            for row in rows:
                lease_until = integer(now_ms + lease_ms)
                connection.execute(
                    "UPDATE assurance_pending_work SET state='RUNNING',row_version=row_version+1,"
                    "tries=tries+1,owner=?,lease_until_ms=? WHERE mission_id=? "
                    "AND consumer=? AND work_key=? AND row_version=?",
                    (owner, lease_until, mission_id, consumer, row["work_key"], row["row_version"]),
                )
                claimed.append(
                    WorkClaim(
                        mission_id,
                        consumer,
                        row["work_key"],
                        row["trigger_event_id"],
                        row["target_epoch"],
                        row["target_fingerprint"],
                        row["row_version"] + 1,
                        row["tries"] + 1,
                        owner,
                        lease_until,
                    )
                )
        return tuple(claimed)

    @staticmethod
    def _assert_claim(connection: sqlite3.Connection, claim: WorkClaim, now_ms: int) -> None:
        row = connection.execute(
            "SELECT * FROM assurance_pending_work WHERE mission_id=? AND consumer=? AND work_key=?",
            (claim.mission_id, claim.consumer, claim.work_key),
        ).fetchone()
        if (
            row is None
            or row["state"] != "RUNNING"
            or any(
                row[key] != value
                for key, value in (
                    ("row_version", claim.row_version),
                    ("owner", claim.owner),
                    ("target_epoch", claim.target_epoch),
                    ("target_fingerprint", claim.target_fingerprint),
                    ("lease_until_ms", claim.lease_until_ms),
                    ("trigger_event_id", claim.trigger_event_id),
                )
            )
            or now_ms >= claim.lease_until_ms
        ):
            raise StoreConflict("assurance work claim superseded or expired")

    def commit(
        self, claim: WorkClaim, *, now_ms: int, effect: Callable[[], T], rejected: bool = False
    ) -> T:
        """Original effect/receipt and ACK share the same transaction.

        The callback MUST revalidate authority, source epochs and expiry and call
        the original Commit writer. Returning an existing receipt still ACKs.
        It must not perform a network operation or await.
        """
        integer(now_ms)
        with atomic(self.store) as connection:
            self._assert_claim(connection, claim, now_ms)
            result = effect()
            # A callback that caused a newer target must not ACK that target.
            self._assert_claim(connection, claim, now_ms)
            connection.execute(
                "UPDATE assurance_pending_work SET state=?,row_version=row_version+1,"
                "owner=NULL,lease_until_ms=NULL,wait_reason=NULL WHERE mission_id=? "
                "AND consumer=? AND work_key=? AND row_version=?",
                (
                    "REJECTED" if rejected else "DONE",
                    claim.mission_id,
                    claim.consumer,
                    claim.work_key,
                    claim.row_version,
                ),
            )
            return result

    def wait(self, claim: WorkClaim, *, now_ms: int, reason: str, not_before_ms: int) -> None:
        integer(now_ms)
        integer(not_before_ms, minimum=now_ms)
        text(reason)
        if reason == "RECHECK_REQUIRED":
            raise AssuranceError("USE_BOUNDED_RECHECK")
        with atomic(self.store) as connection:
            self._assert_claim(connection, claim, now_ms)
            connection.execute(
                "UPDATE assurance_pending_work SET state='WAITING',row_version=row_version+1,"
                "owner=NULL,lease_until_ms=NULL,wait_reason=?,not_before_ms=? WHERE mission_id=? "
                "AND consumer=? AND work_key=? AND row_version=?",
                (
                    reason,
                    not_before_ms,
                    claim.mission_id,
                    claim.consumer,
                    claim.work_key,
                    claim.row_version,
                ),
            )

    def recheck(self, claim: WorkClaim, *, now_ms: int, reason: str = "RECHECK_REQUIRED") -> str:
        """Persist the cumulative recomputation budget across targets/restarts.

        The first three retries can run immediately; then 500/1000/2000ms
        backoff. At 32 failures or 300 seconds the same work waits for a real
        condition change. A newer event may wake it for one useful attempt, but
        another RECHECK_REQUIRED cannot restart this exhausted budget.
        """
        integer(now_ms)
        text(reason)
        if reason == "MANUAL_REQUIRED":
            raise AssuranceError("WORK_RECHECK_REASON_INVALID")
        with atomic(self.store) as connection:
            self._assert_claim(connection, claim, now_ms)
            row = connection.execute(
                "SELECT rechecks,recheck_started_at_ms FROM assurance_pending_work "
                "WHERE mission_id=? AND consumer=? AND work_key=?",
                (claim.mission_id, claim.consumer, claim.work_key),
            ).fetchone()
            started = row["recheck_started_at_ms"]
            if started is None:
                started = now_ms
            attempts = row["rechecks"] + 1
            exhausted = attempts >= 32 or now_ms - started >= 300_000
            if exhausted:
                # The concrete code is overwritten below; keep it diagnosable (2026-09-25
                # desktop run: a JSON_BYTES_LIMIT import failure surfaced only as MANUAL_REQUIRED).
                logging.getLogger(__name__).warning(
                    "assurance work needs manual resolution mission=%s consumer=%s work=%s "
                    "rechecks=%s last_reason=%s", claim.mission_id, claim.consumer,
                    claim.work_key, attempts, reason)
            reason = "MANUAL_REQUIRED" if exhausted else reason
            delay = 0 if attempts <= 3 else (500, 1000, 2000)[min(attempts - 4, 2)]
            due = integer(now_ms + delay)
            connection.execute(
                "UPDATE assurance_pending_work SET state='WAITING',row_version=row_version+1,"
                "owner=NULL,lease_until_ms=NULL,wait_reason=?,not_before_ms=?,rechecks=?,"
                "recheck_started_at_ms=? WHERE mission_id=? AND consumer=? AND work_key=? "
                "AND row_version=?",
                (
                    reason,
                    due,
                    attempts,
                    started,
                    claim.mission_id,
                    claim.consumer,
                    claim.work_key,
                    claim.row_version,
                ),
            )
            return reason
