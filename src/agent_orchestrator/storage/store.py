# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501  (SQL statement literals)

"""``Store``: the orchestrator's SQLite library (Event Store + Current State Store).

Every mutation happens inside ``Store.transaction()`` (BEGIN IMMEDIATE, single
in-process writer lock) and is issued by the Commit Service only.  Entity writes
are compare-and-swap on ``version`` (§17.3): a stale writer gets ``StoreConflict``
instead of a lost update.  Event appends are idempotent on ``idempotency_key``
(§17.4): replaying the same command yields the same event, not a second one.

``fault(point)`` is the failure-injection hook used by the recovery matrix
(ORCH-BUILD §14.2 layer 1): when a point is armed, the orchestrator raises
``InjectedCrash`` there, exactly as a process kill would at that instruction.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from simple_harness.contracts import canonical_json

from ..contracts import (
    Artifact,
    Attempt,
    Claim,
    ContractError,
    Event,
    Mission,
    ResultEnvelope,
    Task,
)
from ..contracts.models import sha256_hex
from ..memory.verified_knowledge import KnowledgeRecord
from . import schema


class StoreError(RuntimeError):
    pass


class StoreConflict(StoreError):
    """A CAS write found a different version than expected (§17.3)."""


class StoreBusy(StoreError):
    """Another instance holds the SQLite write lock (D3-10'); skip this cycle and retry."""


class SchemaIncompatible(StoreError):
    pass


class InjectedCrash(RuntimeError):
    """Raised at an armed fault point; simulates the process dying right there."""

    def __init__(self, point: str) -> None:
        super().__init__(f"injected crash at {point}")
        self.point = point


@dataclass(frozen=True, slots=True)
class StoredResult:
    """A received Result Envelope plus its verification bookkeeping."""

    envelope: ResultEnvelope
    turn_id: str
    verification_state: str
    verdict: str | None
    received_at: float
    artifacts: tuple[str, ...]
    usage_refs: tuple[str, ...]

    def to_json(self) -> dict[str, Any]:
        return {
            "envelope": self.envelope.to_json(),
            "turn_id": self.turn_id,
            "verification_state": self.verification_state,
            "verdict": self.verdict,
            "received_at": self.received_at,
            "artifacts": list(self.artifacts),
            "usage_refs": list(self.usage_refs),
        }


@dataclass(frozen=True, slots=True)
class DispatchIntent:
    """Durable intent to call the SDK with a fixed identity (plan D5)."""

    intent_id: str
    kind: str
    subject_id: str
    mission_id: str
    state: str
    version: int
    creation_key: str
    input_id: str
    input_hash: str
    config: Mapping[str, Any]
    expected_turn_id: str | None
    agent_id: str | None
    receipt: Mapping[str, Any] | None
    lease_owner: str | None
    lease_expires_at: float | None
    replays: int
    created_at: float

    def to_json(self) -> dict[str, Any]:
        return {
            "intent_id": self.intent_id,
            "kind": self.kind,
            "subject_id": self.subject_id,
            "mission_id": self.mission_id,
            "state": self.state,
            "version": self.version,
            "creation_key": self.creation_key,
            "input_id": self.input_id,
            "input_hash": self.input_hash,
            "config": dict(self.config),
            "expected_turn_id": self.expected_turn_id,
            "agent_id": self.agent_id,
            "receipt": None if self.receipt is None else dict(self.receipt),
            "lease_owner": self.lease_owner,
            "lease_expires_at": self.lease_expires_at,
            "replays": self.replays,
            "created_at": self.created_at,
        }


INTENT_STATES = ("PENDING", "CLAIMED", "AGENT_CREATED", "SUBMITTED", "SETTLED", "FAILED")
VERIFICATION_STATES = ("PENDING", "RUNNING", "DONE", "REJECTED")
# P3.1 fix F-ORCH-3: an artifact is UNVERIFIED until its result is judged
ARTIFACT_VERIFICATION_STATES = ("UNVERIFIED", "VERIFIED", "REJECTED")
# P3.2 D4: a workspace directory is CREATING until it is fully built, ACTIVE while it may
# be used, CLEANED once its directory was removed (the row stays)
WORKSPACE_STATES = ("CREATING", "ACTIVE", "CLEANED")
_WORKSPACE_COLUMNS = (
    "workspace_id,kind,mission_id,attempt_id,base_snapshot,state,json,created_at,updated_at"
)


def _workspace_row(row: Any) -> dict[str, Any]:
    return {
        "workspace_id": row[0],
        "kind": row[1],
        "mission_id": row[2],
        "attempt_id": row[3],
        "base_snapshot": row[4],
        "state": row[5],
        "detail": json.loads(row[6]),
        "created_at": row[7],
        "updated_at": row[8],
    }


def _loads(text: str) -> Any:
    return json.loads(text)


class Store:
    """Single-writer access to ``orchestrator.db``."""

    def __init__(self, connection: sqlite3.Connection, path: Path, clock: Callable[[], float]):
        self._connection = connection
        self._path = path
        self._clock = clock
        self._lock = threading.RLock()
        self._depth = 0
        self._holder: object | None = None
        self._reading = False  # host support S2 review P1-B: a read view writes nothing
        self._armed: set[str] = set()
        self._skips: dict[str, int] = {}
        self._times: dict[str, int] = {}
        self.fired: list[str] = []
        self._readonly = False

    # ---------------------------------------------------------------- lifecycle
    @classmethod
    def open(cls, path: str | Path, *, clock: Callable[[], float] = time.time) -> Store:
        resolved = Path(path).expanduser()
        resolved.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            resolved, isolation_level=None, timeout=5.0, check_same_thread=False
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        store = cls(connection, resolved, clock)
        store._initialize_or_validate()
        return store

    @classmethod
    def open_readonly(cls, path: str | Path) -> Store:
        """Step 8 (plan D8-1'): read an existing library without writing it — no
        directory creation, no WAL switch, no migration or backup.  An older schema is
        accepted as it is (``snapshot`` skips tables it does not have); a newer one is
        refused."""

        resolved = Path(path).expanduser().resolve()
        if not resolved.is_file():
            raise StoreError(f"no library at {resolved}")
        connection = sqlite3.connect(
            f"file:{resolved}?mode=ro",
            uri=True,
            isolation_level=None,
            timeout=5.0,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        store = cls(connection, resolved, time.time)
        store._readonly = True
        rows = [
            tuple(row)
            for row in connection.execute(
                "SELECT version,name,checksum FROM orch_schema_migrations ORDER BY version"
            )
        ]
        expected = [(m.version, m.name, m.checksum) for m in schema.MIGRATIONS]
        if len(rows) > len(expected) or rows != expected[: len(rows)]:
            connection.close()
            raise SchemaIncompatible(f"orchestrator schema mismatch: {rows}")
        return store

    def has_table(self, name: str) -> bool:
        row = self._connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?", (name,)
        ).fetchone()
        return row is not None

    def iter_events(self, mission_id: str, *, page: int = 5_000) -> list[Event]:
        """Every event of a Mission, read page by page (plan D8-1': never silently cut
        at a fixed limit)."""

        events: list[Event] = []
        after = 0
        while True:
            batch = self.list_events(mission_id, after_seq=after, limit=page)
            events.extend(batch)
            if len(batch) < page:
                return events
            after = int(batch[-1].seq or after)

    def close(self) -> None:
        self._connection.close()

    @property
    def path(self) -> Path:
        return self._path

    @property
    def now(self) -> float:
        return float(self._clock())

    def _initialize_or_validate(self) -> None:
        tables = {
            row[0]
            for row in self._connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "orch_schema_migrations" not in tables:
            if tables:
                raise SchemaIncompatible("file is not an orchestrator library")
            with self.transaction() as connection:
                for migration in schema.MIGRATIONS:
                    self._apply_migration(connection, migration)
            return
        rows = [
            tuple(row)
            for row in self._connection.execute(
                "SELECT version,name,checksum FROM orch_schema_migrations ORDER BY version"
            )
        ]
        expected = [(m.version, m.name, m.checksum) for m in schema.MIGRATIONS]
        if rows == expected:
            return
        if len(rows) > len(expected) or rows != expected[: len(rows)]:
            raise SchemaIncompatible(f"orchestrator schema mismatch: {rows}")
        # D4-15 / ORCH §12.6: an older library is backed up, then upgraded in place
        pending = schema.MIGRATIONS[len(rows) :]
        if str(self._path) != ":memory:" and self._path.is_file():
            backup = self._path.with_name(
                f"{self._path.name}.pre-schema-{pending[-1].version}.backup"
            )
            if not backup.exists():
                self._connection.commit()
                target = sqlite3.connect(backup)
                try:
                    self._connection.backup(target)
                finally:
                    target.close()
        with self.transaction() as connection:
            for migration in pending:
                self._apply_migration(connection, migration)

    def _apply_migration(self, connection: sqlite3.Connection, migration: schema.Migration) -> None:
        if migration.version == 2:
            self._renumber_artifact_lineage(connection)  # P1-5: v1 libraries may hold duplicates
        for statement in migration.ddl.split(";"):
            if statement.strip():
                connection.execute(statement)
        if migration.version == 6:
            self._bind_legacy_missions(connection)
        connection.execute(
            "INSERT INTO orch_schema_migrations VALUES (?,?,?,?)",
            (migration.version, migration.name, migration.checksum, self.now),
        )

    def _bind_legacy_missions(self, connection: sqlite3.Connection) -> None:
        """Step 9 (plan D9-3', review P1-2): Missions created before policy binding are
        bound to ``policy-legacy`` — no parameters, meaning "the deployment configuration,
        as these Missions always ran" — so an upgraded library still recovers them.
        ``legacy`` is never ACTIVE."""

        missions = [str(row[0]) for row in connection.execute("SELECT mission_id FROM missions")]
        if not missions:
            return
        legacy = schema.LEGACY_POLICY_VERSION
        now = self.now
        record: dict[str, Any] = {
            "version_id": legacy,
            "params_hash": "legacy",
            "params": None,
            "source": "legacy",
            "status": "LEGACY",
            "detail": {"note": "迁移推定：迁移前的 Mission 沿用部署配置运行"},
        }
        connection.execute(
            "INSERT INTO policy_versions(version_id,params_hash,source,status,json,created_at,updated_at)"
            " VALUES (?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
            (legacy, "legacy", "legacy", "LEGACY", canonical_json(record), now, now),
        )
        for mission_id in missions:
            binding: dict[str, Any] = {
                "mission_id": mission_id,
                "version_id": legacy,
                "source": "legacy",
                "provider_kind": "unknown",
            }
            connection.execute(
                "INSERT INTO mission_policies(mission_id,version_id,source,provider_kind,json,bound_at)"
                " VALUES (?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                (mission_id, legacy, "legacy", "unknown", canonical_json(binding), now),
            )

    @staticmethod
    def _renumber_artifact_lineage(connection: sqlite3.Connection) -> None:
        """Step-3 libraries (L3-2) could record two candidates of one path with the same
        version; before the (mission, path, version) index exists, renumber each lineage
        in (created_at, artifact_id) order — ids and hashes are untouched."""

        rows = connection.execute(
            "SELECT artifact_id, mission_id, path, version, json FROM artifacts"
            " ORDER BY mission_id, path, created_at, artifact_id"
        ).fetchall()
        current: tuple[str, str] | None = None
        counter = 0
        for artifact_id, mission_id, path, version, raw in rows:
            key = (mission_id, path)
            if key != current:
                current, counter = key, 0
            counter += 1
            if counter == version:
                continue
            document = _loads(raw)
            document["version"] = counter
            connection.execute(
                "UPDATE artifacts SET version = ?, json = ? WHERE artifact_id = ?",
                (counter, canonical_json(document), artifact_id),
            )

    # ------------------------------------------------------------- transactions
    @staticmethod
    def _current_task() -> object | None:
        try:
            import asyncio

            return asyncio.current_task()
        except RuntimeError:  # no running loop: plain synchronous caller
            return None

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            if self._reading:
                raise StoreError("a read view is open: nothing may be written inside it")
            if self._depth:
                # step 6 (review P1-15): an RLock lets a *different* coroutine of the same
                # thread walk into an open transaction; that would be silent corruption, so a
                # nested entry must come from the task that opened it
                holder = self._holder
                current = self._current_task()
                if holder is not None and current is not None and current is not holder:
                    raise StoreError(
                        "transaction crossed an await: another task entered an open transaction"
                    )
                self._depth += 1
                try:
                    yield self._connection
                finally:
                    self._depth -= 1
                return
            try:
                self._connection.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as error:
                if "locked" in str(error).lower() or "busy" in str(error).lower():
                    raise StoreBusy(str(error)) from error
                raise
            self._depth = 1
            self._holder = self._current_task()
            try:
                yield self._connection
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
            else:
                self._connection.execute("COMMIT")
            finally:
                self._depth = 0
                self._holder = None

    @contextmanager
    def read_view(self) -> Iterator[sqlite3.Connection]:
        """A consistent read (host support S2, P3.1-A05): every SELECT inside sees one
        snapshot of the library, so a Mission snapshot and its event cursor agree.  Inside
        an open transaction it simply joins it.  Like :meth:`transaction` it must never
        span an await.  It writes nothing: a :meth:`transaction` opened inside it is refused
        (review P1-B); an exception rolls the view back, and the state is reset whatever
        COMMIT / ROLLBACK does, so a failed end can never leave later transactions
        non-atomic."""

        with self._lock:
            if self._depth:
                yield self._connection
                return
            self._connection.execute("BEGIN")  # deferred: a read snapshot, no write lock
            self._depth = 1
            self._holder = self._current_task()
            self._reading = True
            try:
                yield self._connection
            except BaseException:
                try:
                    self._connection.execute("ROLLBACK")
                finally:
                    self._depth, self._holder, self._reading = 0, None, False
                raise
            else:
                try:
                    self._connection.execute("COMMIT")
                finally:
                    self._depth, self._holder, self._reading = 0, None, False

    @property
    def connection(self) -> sqlite3.Connection:
        return self._connection

    # ---------------------------------------------------------- fault injection
    def arm(self, *points: str, skip: int = 0, times: int = 1) -> None:
        """Arm crash points; ``skip`` lets the first ``skip`` hits pass (crash on the next);
        ``times`` keeps the point armed for that many crashes (step 4: repeated retrieval
        failures)."""

        self._armed.update(points)
        for point in points:
            self._skips[point] = skip
            self._times[point] = max(1, times)

    def disarm(self, *points: str) -> None:
        if points:
            self._armed.difference_update(points)
        else:
            self._armed.clear()

    def fault(self, point: str, kind: str | None = None) -> None:
        """Crash here if ``point`` (or ``point:kind``) is armed; one shot per arming."""

        for candidate in (point, f"{point}:{kind}") if kind else (point,):
            if candidate in self._armed:
                if self._skips.get(candidate, 0) > 0:
                    self._skips[candidate] -= 1
                    continue
                remaining = self._times.get(candidate, 1) - 1
                if remaining <= 0:
                    self._armed.discard(candidate)
                else:
                    self._times[candidate] = remaining
                self.fired.append(candidate)
                raise InjectedCrash(candidate)

    # ----------------------------------------------------------------- events
    def append_event(self, event: Event) -> Event:
        """Idempotent append: an existing ``idempotency_key`` returns the stored event."""

        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM events WHERE idempotency_key = ?", (event.idempotency_key,)
            ).fetchone()
            if existing is not None:
                return _event_from_row(existing)
            cursor = connection.execute(
                "INSERT INTO events(event_id,idempotency_key,type,trace_id,mission_id,task_id,"
                "attempt_id,actor_type,actor_id,payload_json,created_at,schema_version)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    event.id,
                    event.idempotency_key,
                    event.type,
                    event.trace_id,
                    event.mission_id,
                    event.task_id,
                    event.attempt_id,
                    event.actor_type,
                    event.actor_id,
                    canonical_json(dict(event.payload)),
                    event.created_at,
                    event.schema_version,
                ),
            )
            return Event(**{**event.to_json(), "seq": cursor.lastrowid})

    def list_events(
        self, mission_id: str, *, after_seq: int = 0, limit: int = 10_000
    ) -> list[Event]:
        rows = self._connection.execute(
            "SELECT * FROM events WHERE mission_id = ? AND seq > ? ORDER BY seq LIMIT ?",
            (mission_id, after_seq, limit),
        ).fetchall()
        return [_event_from_row(row) for row in rows]

    def count_events(self, mission_id: str, event_type: str | None = None) -> int:
        if event_type is None:
            row = self._connection.execute(
                "SELECT COUNT(*) FROM events WHERE mission_id = ?", (mission_id,)
            ).fetchone()
        else:
            row = self._connection.execute(
                "SELECT COUNT(*) FROM events WHERE mission_id = ? AND type = ?",
                (mission_id, event_type),
            ).fetchone()
        return int(row[0])

    def last_event_seq(self, mission_id: str) -> int:
        """The Mission's highest event ``seq`` (0 when none) — a snapshot's cursor."""

        row = self._connection.execute(
            "SELECT COALESCE(MAX(seq), 0) FROM events WHERE mission_id = ?", (mission_id,)
        ).fetchone()
        return int(row[0])

    # --------------------------------------------------------------- missions
    def insert_mission(self, mission: Mission, *, spec_hash: str) -> None:
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO missions(mission_id,tenant_id,idempotency_key,status,version,"
                "spec_hash,json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    mission.id,
                    mission.tenant_id,
                    mission.idempotency_key,
                    str(mission.status),
                    mission.version,
                    spec_hash,
                    canonical_json(mission.to_json()),
                    mission.created_at,
                    self.now,
                ),
            )

    def update_mission(self, mission: Mission, *, expected_version: int) -> None:
        self._cas(
            "missions",
            "mission_id",
            mission.id,
            expected_version,
            {
                "status": str(mission.status),
                "version": mission.version,
                "json": canonical_json(mission.to_json()),
            },
        )

    def get_mission(self, mission_id: str) -> Mission | None:
        row = self._connection.execute(
            "SELECT json FROM missions WHERE mission_id = ?", (mission_id,)
        ).fetchone()
        return None if row is None else Mission.from_json(_loads(row[0]))

    def find_mission(self, tenant_id: str, idempotency_key: str) -> tuple[Mission, str] | None:
        row = self._connection.execute(
            "SELECT json, spec_hash FROM missions WHERE tenant_id = ? AND idempotency_key = ?",
            (tenant_id, idempotency_key),
        ).fetchone()
        return None if row is None else (Mission.from_json(_loads(row[0])), str(row[1]))

    def list_missions(self, *, statuses: tuple[str, ...] | None = None) -> list[Mission]:
        if statuses:
            marks = ",".join("?" for _ in statuses)
            rows = self._connection.execute(
                f"SELECT json FROM missions WHERE status IN ({marks}) ORDER BY created_at",
                tuple(statuses),
            ).fetchall()
        else:
            rows = self._connection.execute(
                "SELECT json FROM missions ORDER BY created_at"
            ).fetchall()
        return [Mission.from_json(_loads(row[0])) for row in rows]

    # ------------------------------------------------------------------ tasks
    def insert_task(self, task: Task, *, ordinal: int) -> None:
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO tasks(task_id,mission_id,ordinal,status,version,json,updated_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (
                    task.id,
                    task.mission_id,
                    ordinal,
                    str(task.status),
                    task.version,
                    canonical_json(task.to_json()),
                    self.now,
                ),
            )

    def update_task(self, task: Task, *, expected_version: int) -> None:
        self._cas(
            "tasks",
            "task_id",
            task.id,
            expected_version,
            {
                "status": str(task.status),
                "version": task.version,
                "json": canonical_json(task.to_json()),
            },
        )

    def get_task(self, task_id: str) -> Task | None:
        row = self._connection.execute(
            "SELECT json FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        return None if row is None else Task.from_json(_loads(row[0]))

    def list_tasks(self, mission_id: str) -> list[Task]:
        rows = self._connection.execute(
            "SELECT json FROM tasks WHERE mission_id = ? ORDER BY ordinal", (mission_id,)
        ).fetchall()
        return [Task.from_json(_loads(row[0])) for row in rows]

    # --------------------------------------------------------------- attempts
    def insert_attempt(self, attempt: Attempt) -> None:
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO attempts(attempt_id,task_id,mission_id,ordinal,status,version,"
                "lease_owner,lease_expires_at,agent_id,turn_id,json,updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    attempt.id,
                    attempt.task_id,
                    attempt.mission_id,
                    attempt.ordinal,
                    str(attempt.status),
                    attempt.version,
                    attempt.lease_owner,
                    attempt.lease_expires_at,
                    attempt.agent_id,
                    attempt.turn_id,
                    canonical_json(attempt.to_json()),
                    self.now,
                ),
            )

    def update_attempt(self, attempt: Attempt, *, expected_version: int) -> None:
        self._cas(
            "attempts",
            "attempt_id",
            attempt.id,
            expected_version,
            {
                "status": str(attempt.status),
                "version": attempt.version,
                "lease_owner": attempt.lease_owner,
                "lease_expires_at": attempt.lease_expires_at,
                "agent_id": attempt.agent_id,
                "turn_id": attempt.turn_id,
                "json": canonical_json(attempt.to_json()),
            },
        )

    def get_attempt(self, attempt_id: str) -> Attempt | None:
        row = self._connection.execute(
            "SELECT json FROM attempts WHERE attempt_id = ?", (attempt_id,)
        ).fetchone()
        return None if row is None else Attempt.from_json(_loads(row[0]))

    def list_attempts(self, task_id: str) -> list[Attempt]:
        rows = self._connection.execute(
            "SELECT json FROM attempts WHERE task_id = ? ORDER BY ordinal", (task_id,)
        ).fetchall()
        return [Attempt.from_json(_loads(row[0])) for row in rows]

    def count_attempts_by_status(self, *statuses: str) -> int:
        """Attempts in any of ``statuses`` across every Mission (step 6: the global cap)."""

        if not statuses:
            return 0
        marks = ",".join("?" for _ in statuses)
        row = self._connection.execute(
            f"SELECT COUNT(*) FROM attempts WHERE status IN ({marks})", tuple(statuses)
        ).fetchone()
        return int(row[0])

    def list_attempts_by_status(self, *statuses: str) -> list[Attempt]:
        marks = ",".join("?" for _ in statuses)
        rows = self._connection.execute(
            f"SELECT json FROM attempts WHERE status IN ({marks}) ORDER BY updated_at",
            tuple(statuses),
        ).fetchall()
        return [Attempt.from_json(_loads(row[0])) for row in rows]

    # -------------------------------------------------------- dispatch intents
    def insert_intent(self, intent: DispatchIntent) -> None:
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO dispatch_intents(intent_id,kind,subject_id,mission_id,state,version,"
                "creation_key,input_id,input_hash,config_json,expected_turn_id,agent_id,receipt_json,"
                "lease_owner,lease_expires_at,replays,created_at,updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    intent.intent_id,
                    intent.kind,
                    intent.subject_id,
                    intent.mission_id,
                    intent.state,
                    intent.version,
                    intent.creation_key,
                    intent.input_id,
                    intent.input_hash,
                    canonical_json(dict(intent.config)),
                    intent.expected_turn_id,
                    intent.agent_id,
                    None if intent.receipt is None else canonical_json(dict(intent.receipt)),
                    intent.lease_owner,
                    intent.lease_expires_at,
                    intent.replays,
                    intent.created_at,
                    self.now,
                ),
            )

    def update_intent(self, intent: DispatchIntent, *, expected_version: int) -> None:
        self._cas(
            "dispatch_intents",
            "intent_id",
            intent.intent_id,
            expected_version,
            {
                "state": intent.state,
                "version": intent.version,
                "expected_turn_id": intent.expected_turn_id,
                "agent_id": intent.agent_id,
                "receipt_json": None
                if intent.receipt is None
                else canonical_json(dict(intent.receipt)),
                "lease_owner": intent.lease_owner,
                "lease_expires_at": intent.lease_expires_at,
                "replays": intent.replays,
            },
        )

    def get_intent(self, intent_id: str) -> DispatchIntent | None:
        row = self._connection.execute(
            "SELECT * FROM dispatch_intents WHERE intent_id = ?", (intent_id,)
        ).fetchone()
        return None if row is None else _intent_from_row(row)

    def get_intent_for_subject(self, subject_id: str) -> DispatchIntent | None:
        row = self._connection.execute(
            "SELECT * FROM dispatch_intents WHERE subject_id = ?", (subject_id,)
        ).fetchone()
        return None if row is None else _intent_from_row(row)

    def list_intents(self, *states: str) -> list[DispatchIntent]:
        marks = ",".join("?" for _ in states)
        rows = self._connection.execute(
            f"SELECT * FROM dispatch_intents WHERE state IN ({marks}) ORDER BY created_at",
            tuple(states),
        ).fetchall()
        return [_intent_from_row(row) for row in rows]

    # ----------------------------------------------------------------- results
    def insert_result(self, stored: StoredResult) -> None:
        envelope = stored.envelope
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO results(result_id,attempt_id,task_id,mission_id,turn_id,result_hash,"
                "verification_state,verdict,json,received_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    envelope.id,
                    envelope.attempt_id,
                    envelope.task_id,
                    envelope.mission_id,
                    stored.turn_id,
                    envelope.result_hash,
                    stored.verification_state,
                    stored.verdict,
                    canonical_json(stored.to_json()),
                    stored.received_at,
                    self.now,
                ),
            )

    def set_result_verification(self, result_id: str, *, state: str, verdict: str | None) -> None:
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT json FROM results WHERE result_id = ?", (result_id,)
            ).fetchone()
            if row is None:
                raise StoreError(f"unknown result {result_id}")
            data = _loads(row[0])
            data["verification_state"] = state
            data["verdict"] = verdict
            connection.execute(
                "UPDATE results SET verification_state = ?, verdict = ?, json = ?, updated_at = ?"
                " WHERE result_id = ?",
                (state, verdict, canonical_json(data), self.now, result_id),
            )

    def get_result(self, result_id: str) -> StoredResult | None:
        row = self._connection.execute(
            "SELECT json FROM results WHERE result_id = ?", (result_id,)
        ).fetchone()
        return None if row is None else _stored_result(_loads(row[0]))

    def find_result_for_attempt(self, attempt_id: str) -> StoredResult | None:
        row = self._connection.execute(
            "SELECT json FROM results WHERE attempt_id = ? ORDER BY received_at DESC LIMIT 1",
            (attempt_id,),
        ).fetchone()
        return None if row is None else _stored_result(_loads(row[0]))

    def list_results_by_verification(self, *states: str) -> list[StoredResult]:
        marks = ",".join("?" for _ in states)
        rows = self._connection.execute(
            f"SELECT json FROM results WHERE verification_state IN ({marks}) ORDER BY received_at",
            tuple(states),
        ).fetchall()
        return [_stored_result(_loads(row[0])) for row in rows]

    # ----------------------------------------------------------- verifications
    def upsert_verification(
        self, *, result_id: str, attempt_id: str, layer: str, status: str, detail: Mapping[str, Any]
    ) -> None:
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO verifications(verification_id,result_id,attempt_id,layer,status,detail_json,created_at)"
                " VALUES (?,?,?,?,?,?,?) ON CONFLICT(result_id, layer) DO UPDATE SET"
                " status = excluded.status, detail_json = excluded.detail_json",
                (
                    f"{result_id}:{layer}",
                    result_id,
                    attempt_id,
                    layer,
                    status,
                    canonical_json(dict(detail)),
                    self.now,
                ),
            )

    def list_verifications(self, result_id: str) -> list[dict[str, Any]]:
        rows = self._connection.execute(
            "SELECT layer, status, detail_json, created_at FROM verifications WHERE result_id = ?"
            " ORDER BY created_at, layer",
            (result_id,),
        ).fetchall()
        return [
            {"layer": row[0], "status": row[1], "detail": _loads(row[2]), "created_at": row[3]}
            for row in rows
        ]

    # ------------------------------------------------------------------ claims
    def upsert_claim(self, claim: Claim) -> None:
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO claims(claim_id,mission_id,result_id,status,version,json,updated_at,key)"
                " VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(claim_id) DO UPDATE SET status = excluded.status,"
                " version = excluded.version, json = excluded.json, updated_at = excluded.updated_at,"
                " key = excluded.key",
                (
                    claim.id,
                    claim.mission_id,
                    claim.result_id,
                    str(claim.status),
                    claim.version,
                    canonical_json(claim.to_json()),
                    self.now,
                    claim.key,
                ),
            )

    def get_claim(self, claim_id: str) -> Claim | None:
        row = self._connection.execute(
            "SELECT json FROM claims WHERE claim_id = ?", (claim_id,)
        ).fetchone()
        return None if row is None else Claim.from_json(_loads(row[0]))

    def list_claims(self, result_id: str) -> list[Claim]:
        rows = self._connection.execute(
            "SELECT json FROM claims WHERE result_id = ? ORDER BY claim_id", (result_id,)
        ).fetchall()
        return [Claim.from_json(_loads(row[0])) for row in rows]

    def list_mission_claims(self, mission_id: str) -> list[Claim]:
        rows = self._connection.execute(
            "SELECT json FROM claims WHERE mission_id = ? ORDER BY claim_id", (mission_id,)
        ).fetchall()
        return [Claim.from_json(_loads(row[0])) for row in rows]

    # --------------------------------------------------------------- knowledge
    def upsert_knowledge(self, record: KnowledgeRecord) -> None:
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO knowledge(knowledge_id,mission_id,claim_id,key,status,version,source_task,json,created_at,updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(knowledge_id) DO UPDATE SET status = excluded.status,"
                " version = excluded.version, json = excluded.json, updated_at = excluded.updated_at",
                (
                    record.id,
                    record.mission_id,
                    record.claim_id,
                    record.key,
                    record.status,
                    record.version,
                    record.source_task,
                    canonical_json(record.to_json()),
                    record.created_at,
                    self.now,
                ),
            )

    def get_knowledge(self, knowledge_id: str) -> KnowledgeRecord | None:
        row = self._connection.execute(
            "SELECT json FROM knowledge WHERE knowledge_id = ?", (knowledge_id,)
        ).fetchone()
        return None if row is None else KnowledgeRecord.from_json(_loads(row[0]))

    def list_knowledge(
        self, mission_id: str, *, status: str | None = None
    ) -> list[KnowledgeRecord]:
        """Knowledge of one Mission only — the Mission boundary is the permission
        pre-filter of retrieval (S4-06)."""

        if status is None:
            rows = self._connection.execute(
                "SELECT json FROM knowledge WHERE mission_id = ? ORDER BY created_at, knowledge_id",
                (mission_id,),
            ).fetchall()
        else:
            rows = self._connection.execute(
                "SELECT json FROM knowledge WHERE mission_id = ? AND status = ? ORDER BY created_at, knowledge_id",
                (mission_id, status),
            ).fetchall()
        return [KnowledgeRecord.from_json(_loads(row[0])) for row in rows]

    # --------------------------------------------------------------- summaries
    def upsert_summary(
        self,
        mission_id: str,
        *,
        scope: str,
        subject_id: str,
        version: str,
        summary: Mapping[str, Any],
    ) -> None:
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO summaries(summary_id,mission_id,scope,subject_id,version,json,created_at)"
                " VALUES (?,?,?,?,?,?,?) ON CONFLICT(mission_id, scope, subject_id) DO UPDATE SET"
                " version = excluded.version, json = excluded.json, created_at = excluded.created_at",
                (
                    f"{mission_id}:{scope}:{subject_id}",
                    mission_id,
                    scope,
                    subject_id,
                    version,
                    canonical_json(dict(summary)),
                    self.now,
                ),
            )

    def list_summaries(self, mission_id: str, *, scope: str | None = None) -> list[dict[str, Any]]:
        if scope is None:
            rows = self._connection.execute(
                "SELECT json FROM summaries WHERE mission_id = ? ORDER BY scope, subject_id",
                (mission_id,),
            ).fetchall()
        else:
            rows = self._connection.execute(
                "SELECT json FROM summaries WHERE mission_id = ? AND scope = ? ORDER BY subject_id",
                (mission_id, scope),
            ).fetchall()
        return [_loads(row[0]) for row in rows]

    # ----------------------------------------------------------- graph changes
    # --------------------------------------------------------------- actions (step 7)
    def put_action(self, record: Mapping[str, Any]) -> None:
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO actions(action_key,action_id,version,mission_id,state,json,created_at,updated_at)"
                " VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(action_key) DO UPDATE SET state = excluded.state,"
                " json = excluded.json, updated_at = excluded.updated_at",
                (
                    str(record["action_key"]),
                    str(record["action_id"]),
                    int(record["version"]),
                    str(record["mission_id"]),
                    str(record["state"]),
                    canonical_json(dict(record)),
                    self.now,
                    self.now,
                ),
            )

    def get_action(self, action_key: str) -> dict[str, Any] | None:
        row = self._connection.execute(
            "SELECT json FROM actions WHERE action_key = ?", (action_key,)
        ).fetchone()
        return None if row is None else dict(_loads(row[0]))

    def list_action_versions(self, action_id: str) -> list[dict[str, Any]]:
        rows = self._connection.execute(
            "SELECT json FROM actions WHERE action_id = ? ORDER BY version", (action_id,)
        ).fetchall()
        return [dict(_loads(row[0])) for row in rows]

    def list_actions(self, mission_id: str | None = None, *states: str) -> list[dict[str, Any]]:
        sql = "SELECT json FROM actions"
        clauses: list[str] = []
        args: list[Any] = []
        if mission_id is not None:
            clauses.append("mission_id = ?")
            args.append(mission_id)
        if states:
            clauses.append(f"state IN ({','.join('?' for _ in states)})")
            args.extend(states)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        rows = self._connection.execute(
            sql + " ORDER BY created_at, action_key", tuple(args)
        ).fetchall()
        return [dict(_loads(row[0])) for row in rows]

    def put_approval(self, record: Mapping[str, Any]) -> None:
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO approvals(request_id,kind,mission_id,subject_key,state,version,json,created_at,updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(request_id) DO UPDATE SET state = excluded.state,"
                " version = excluded.version, json = excluded.json, updated_at = excluded.updated_at",
                (
                    str(record["request_id"]),
                    str(record["kind"]),
                    str(record["mission_id"]),
                    str(record["subject_key"]),
                    str(record["state"]),
                    int(record["version"]),
                    canonical_json(dict(record)),
                    self.now,
                    self.now,
                ),
            )

    def get_approval(self, request_id: str) -> dict[str, Any] | None:
        row = self._connection.execute(
            "SELECT json FROM approvals WHERE request_id = ?", (request_id,)
        ).fetchone()
        return None if row is None else dict(_loads(row[0]))

    def list_approvals(self, mission_id: str | None = None, *states: str) -> list[dict[str, Any]]:
        sql = "SELECT json FROM approvals"
        clauses: list[str] = []
        args: list[Any] = []
        if mission_id is not None:
            clauses.append("mission_id = ?")
            args.append(mission_id)
        if states:
            clauses.append(f"state IN ({','.join('?' for _ in states)})")
            args.extend(states)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        rows = self._connection.execute(
            sql + " ORDER BY created_at, request_id", tuple(args)
        ).fetchall()
        return [dict(_loads(row[0])) for row in rows]

    def insert_decision(self, record: Mapping[str, Any]) -> bool:
        with self.transaction() as connection:
            cursor = connection.execute(
                "INSERT INTO approval_decisions(receipt_hash,request_id,principal_id,decision,nonce,json,created_at)"
                " VALUES (?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                (
                    str(record["receipt_hash"]),
                    str(record["request_id"]),
                    str(record["principal_id"]),
                    str(record["decision"]),
                    str(record["nonce"]),
                    canonical_json(dict(record)),
                    self.now,
                ),
            )
            return cursor.rowcount == 1

    def list_decisions(self, request_id: str) -> list[dict[str, Any]]:
        rows = self._connection.execute(
            "SELECT json FROM approval_decisions WHERE request_id = ? ORDER BY created_at, receipt_hash",
            (request_id,),
        ).fetchall()
        return [dict(_loads(row[0])) for row in rows]

    def insert_override(self, record: Mapping[str, Any]) -> None:
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO human_overrides(override_id,mission_id,json,created_at) VALUES (?,?,?,?)"
                " ON CONFLICT(override_id) DO NOTHING",
                (
                    str(record["override_id"]),
                    str(record["mission_id"]),
                    canonical_json(dict(record)),
                    self.now,
                ),
            )

    def list_overrides(self, mission_id: str) -> list[dict[str, Any]]:
        rows = self._connection.execute(
            "SELECT json FROM human_overrides WHERE mission_id = ? ORDER BY created_at, override_id",
            (mission_id,),
        ).fetchall()
        return [dict(_loads(row[0])) for row in rows]

    # ------------------------------------------------------------ tool calls
    def record_tool_call(
        self, *, call_key: str, subject_id: str, mission_id: str, tool: str, outcome: str
    ) -> bool:
        """One executed tool call, at most once per SDK call id (review P1-3)."""

        with self.transaction() as connection:
            cursor = connection.execute(
                "INSERT INTO tool_calls(call_key,subject_id,mission_id,tool,outcome,created_at)"
                " VALUES (?,?,?,?,?,?) ON CONFLICT(call_key) DO NOTHING",
                (call_key, subject_id, mission_id, tool, outcome, self.now),
            )
            return cursor.rowcount == 1

    def count_tool_calls(self, subject_id: str, *, outcome: str = "succeeded") -> int:
        row = self._connection.execute(
            "SELECT COUNT(*) FROM tool_calls WHERE subject_id = ? AND outcome = ?",
            (subject_id, outcome),
        ).fetchone()
        return int(row[0])

    # --------------------------------------------------------- scheduler state
    def get_scheduler_state(self, key: str) -> dict[str, Any] | None:
        row = self._connection.execute(
            "SELECT json FROM scheduler_state WHERE key = ?", (key,)
        ).fetchone()
        return None if row is None else dict(_loads(row[0]))

    def put_scheduler_state(self, key: str, value: Mapping[str, Any]) -> int:
        """Upsert; returns the new version.  Inside a Commit Service transaction."""

        with self.transaction() as connection:
            row = connection.execute(
                "SELECT version FROM scheduler_state WHERE key = ?", (key,)
            ).fetchone()
            version = 1 if row is None else int(row[0]) + 1
            connection.execute(
                "INSERT INTO scheduler_state(key,json,version,updated_at) VALUES (?,?,?,?)"
                " ON CONFLICT(key) DO UPDATE SET json = excluded.json, version = excluded.version,"
                " updated_at = excluded.updated_at",
                (key, canonical_json(dict(value)), version, self.now),
            )
            return version

    # --------------------------------------------------------- policy registry (step 9)
    def insert_policy_version(self, record: Mapping[str, Any]) -> bool:
        with self.transaction() as connection:
            cursor = connection.execute(
                "INSERT INTO policy_versions(version_id,params_hash,source,status,json,created_at,updated_at)"
                " VALUES (?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                (
                    str(record["version_id"]),
                    str(record["params_hash"]),
                    str(record["source"]),
                    str(record["status"]),
                    canonical_json(dict(record)),
                    self.now,
                    self.now,
                ),
            )
            return cursor.rowcount == 1

    def set_policy_version_status(self, version_id: str, status: str) -> None:
        with self.transaction() as connection:
            record = self.get_policy_version(version_id)
            if record is None:
                raise StoreError(f"unknown policy version {version_id}")
            record["status"] = status
            connection.execute(
                "UPDATE policy_versions SET status = ?, json = ?, updated_at = ? WHERE version_id = ?",
                (status, canonical_json(record), self.now, version_id),
            )

    def get_policy_version(self, version_id: str) -> dict[str, Any] | None:
        row = self._connection.execute(
            "SELECT json FROM policy_versions WHERE version_id = ?", (version_id,)
        ).fetchone()
        return None if row is None else dict(_loads(row[0]))

    def list_policy_versions(self) -> list[dict[str, Any]]:
        rows = self._connection.execute(
            "SELECT json FROM policy_versions ORDER BY created_at, version_id"
        ).fetchall()
        return [dict(_loads(row[0])) for row in rows]

    def insert_policy_proposal(self, record: Mapping[str, Any]) -> bool:
        with self.transaction() as connection:
            cursor = connection.execute(
                "INSERT INTO policy_proposals(proposal_id,version_id,state,json,created_at,updated_at)"
                " VALUES (?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                (
                    str(record["proposal_id"]),
                    str(record["version_id"]),
                    str(record["state"]),
                    canonical_json(dict(record)),
                    self.now,
                    self.now,
                ),
            )
            return cursor.rowcount == 1

    def update_policy_proposal(self, record: Mapping[str, Any]) -> None:
        with self.transaction() as connection:
            connection.execute(
                "UPDATE policy_proposals SET state = ?, json = ?, updated_at = ? WHERE proposal_id = ?",
                (
                    str(record["state"]),
                    canonical_json(dict(record)),
                    self.now,
                    str(record["proposal_id"]),
                ),
            )

    def get_policy_proposal(self, proposal_id: str) -> dict[str, Any] | None:
        row = self._connection.execute(
            "SELECT json FROM policy_proposals WHERE proposal_id = ?", (proposal_id,)
        ).fetchone()
        return None if row is None else dict(_loads(row[0]))

    def list_policy_proposals(self) -> list[dict[str, Any]]:
        rows = self._connection.execute(
            "SELECT json FROM policy_proposals ORDER BY created_at, proposal_id"
        ).fetchall()
        return [dict(_loads(row[0])) for row in rows]

    def insert_policy_evaluation(self, record: Mapping[str, Any]) -> bool:
        with self.transaction() as connection:
            cursor = connection.execute(
                "INSERT INTO policy_evaluations(evaluation_id,proposal_id,verdict,json,created_at)"
                " VALUES (?,?,?,?,?) ON CONFLICT DO NOTHING",
                (
                    str(record["evaluation_id"]),
                    str(record["proposal_id"]),
                    str(record["verdict"]),
                    canonical_json(dict(record)),
                    self.now,
                ),
            )
            return cursor.rowcount == 1

    def list_policy_evaluations(self, proposal_id: str) -> list[dict[str, Any]]:
        rows = self._connection.execute(
            "SELECT json FROM policy_evaluations WHERE proposal_id = ? ORDER BY rowid",
            (proposal_id,),
        ).fetchall()
        return [dict(_loads(row[0])) for row in rows]

    def insert_policy_decision(self, record: Mapping[str, Any]) -> bool:
        with self.transaction() as connection:
            cursor = connection.execute(
                "INSERT INTO policy_decisions(receipt_hash,proposal_id,principal_id,decision,nonce,json,created_at)"
                " VALUES (?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                (
                    str(record["receipt_hash"]),
                    str(record["proposal_id"]),
                    str(record["principal_id"]),
                    str(record["decision"]),
                    str(record["nonce"]),
                    canonical_json(dict(record)),
                    self.now,
                ),
            )
            return cursor.rowcount == 1

    def list_policy_decisions(self, proposal_id: str) -> list[dict[str, Any]]:
        rows = self._connection.execute(
            "SELECT json FROM policy_decisions WHERE proposal_id = ? ORDER BY rowid",
            (proposal_id,),
        ).fetchall()
        return [dict(_loads(row[0])) for row in rows]

    def insert_policy_activation(self, record: Mapping[str, Any]) -> int:
        with self.transaction() as connection:
            cursor = connection.execute(
                "INSERT INTO policy_activations(version_id,action,json,created_at) VALUES (?,?,?,?)",
                (
                    str(record["version_id"]),
                    str(record["action"]),
                    canonical_json(dict(record)),
                    self.now,
                ),
            )
            return int(cursor.lastrowid or 0)

    def list_policy_activations(self) -> list[dict[str, Any]]:
        if not self.has_table("policy_activations"):
            return []
        rows = self._connection.execute(
            "SELECT seq, json, created_at FROM policy_activations ORDER BY seq"
        ).fetchall()
        return [
            {**dict(_loads(row[1])), "seq": int(row[0]), "created_at": float(row[2])}
            for row in rows
        ]

    def active_policy(self) -> dict[str, Any] | None:
        """The version the last activation made ACTIVE (plan D9-2'), or None."""

        if not self.has_table("policy_activations"):
            return None
        row = self._connection.execute(
            "SELECT version_id FROM policy_activations ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        return None if row is None else self.get_policy_version(str(row[0]))

    def bind_mission_policy(self, record: Mapping[str, Any]) -> bool:
        with self.transaction() as connection:
            cursor = connection.execute(
                "INSERT INTO mission_policies(mission_id,version_id,source,provider_kind,json,bound_at)"
                " VALUES (?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                (
                    str(record["mission_id"]),
                    str(record["version_id"]),
                    str(record["source"]),
                    str(record["provider_kind"]),
                    canonical_json(dict(record)),
                    self.now,
                ),
            )
            return cursor.rowcount == 1

    def get_mission_policy(self, mission_id: str) -> dict[str, Any] | None:
        if not self.has_table("mission_policies"):
            return None
        row = self._connection.execute(
            "SELECT json FROM mission_policies WHERE mission_id = ?", (mission_id,)
        ).fetchone()
        return None if row is None else dict(_loads(row[0]))

    # ---------------------------------------------------------- domains (P3.3 D1)

    def bind_mission_domain(
        self, mission_id: str, *, domain_id: str, domain_version: str, snapshot: Mapping[str, Any]
    ) -> bool:
        """Freeze this Mission's domain profile.  The snapshot is a copy of the profile's
        content: a later change to the registry never moves a running Mission."""

        with self.transaction() as connection:
            cursor = connection.execute(
                "INSERT INTO mission_domains(mission_id,domain_id,domain_version,json,bound_at)"
                " VALUES (?,?,?,?,?) ON CONFLICT DO NOTHING",
                (
                    str(mission_id),
                    str(domain_id),
                    str(domain_version),
                    canonical_json(dict(snapshot)),
                    self.now,
                ),
            )
            return cursor.rowcount == 1

    def get_mission_domain(self, mission_id: str) -> dict[str, Any] | None:
        if not self.has_table("mission_domains"):
            return None  # a library from before schema v8
        row = self._connection.execute(
            "SELECT domain_id,domain_version,json,bound_at FROM mission_domains WHERE mission_id = ?",
            (mission_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "mission_id": mission_id,
            "domain_id": row[0],
            "domain_version": row[1],
            "json": dict(_loads(row[2])),
            "bound_at": row[3],
        }

    def list_mission_policies(self, version_id: str | None = None) -> list[dict[str, Any]]:
        if not self.has_table("mission_policies"):
            return []
        sql = "SELECT json FROM mission_policies"
        args: tuple[Any, ...] = ()
        if version_id is not None:
            sql, args = sql + " WHERE version_id = ?", (version_id,)
        rows = self._connection.execute(sql + " ORDER BY bound_at, mission_id", args).fetchall()
        return [dict(_loads(row[0])) for row in rows]

    def has_non_fixture_missions(self) -> bool:
        """Plan D9-8': has this deployment ever bound a Mission that did not run on
        fixtures (a real model, or a kind nobody recorded)?"""

        if not self.has_table("mission_policies"):
            return False
        row = self._connection.execute(
            "SELECT 1 FROM mission_policies WHERE provider_kind != 'fixtures' LIMIT 1"
        ).fetchone()
        return row is not None

    def insert_graph_change(self, record: Mapping[str, Any]) -> None:
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO graph_changes(change_id,mission_id,from_version,to_version,proposal_hash,json,created_at)"
                " VALUES (?,?,?,?,?,?,?) ON CONFLICT(change_id) DO NOTHING",
                (
                    str(record["change_id"]),
                    str(record["mission_id"]),
                    int(record["from_version"]),
                    int(record["to_version"]),
                    str(record["proposal_hash"]),
                    canonical_json(dict(record)),
                    self.now,
                ),
            )

    def get_graph_change(self, change_id: str) -> dict[str, Any] | None:
        row = self._connection.execute(
            "SELECT json FROM graph_changes WHERE change_id = ?", (change_id,)
        ).fetchone()
        return None if row is None else _loads(row[0])

    def list_graph_changes(
        self, mission_id: str, *, since_version: int = 0
    ) -> list[dict[str, Any]]:
        rows = self._connection.execute(
            "SELECT json FROM graph_changes WHERE mission_id = ? AND from_version >= ? ORDER BY to_version",
            (mission_id, since_version),
        ).fetchall()
        return [_loads(row[0]) for row in rows]

    # --------------------------------------------------------------- conflicts
    def upsert_conflict(self, conflict: Mapping[str, Any]) -> None:
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO conflicts(conflict_id,mission_id,key,state,task_id,version,json,created_at,updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(conflict_id) DO UPDATE SET state = excluded.state,"
                " task_id = excluded.task_id, version = excluded.version, json = excluded.json,"
                " updated_at = excluded.updated_at",
                (
                    str(conflict["conflict_id"]),
                    str(conflict["mission_id"]),
                    str(conflict["key"]),
                    str(conflict["state"]),
                    conflict.get("task_id"),
                    int(conflict.get("version", 1)),
                    canonical_json(dict(conflict)),
                    float(conflict.get("created_at") or self.now),
                    self.now,
                ),
            )

    def get_conflict(self, conflict_id: str) -> dict[str, Any] | None:
        row = self._connection.execute(
            "SELECT json FROM conflicts WHERE conflict_id = ?", (conflict_id,)
        ).fetchone()
        return None if row is None else _loads(row[0])

    def list_conflicts(self, mission_id: str, *, state: str | None = None) -> list[dict[str, Any]]:
        if state is None:
            rows = self._connection.execute(
                "SELECT json FROM conflicts WHERE mission_id = ? ORDER BY created_at, conflict_id",
                (mission_id,),
            ).fetchall()
        else:
            rows = self._connection.execute(
                "SELECT json FROM conflicts WHERE mission_id = ? AND state = ? ORDER BY created_at, conflict_id",
                (mission_id, state),
            ).fetchall()
        return [_loads(row[0]) for row in rows]

    # --------------------------------------------------------------- artifacts
    def upsert_artifact(self, artifact: Artifact) -> None:
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO artifacts(artifact_id,mission_id,task_id,attempt_id,path,content_hash,version,"
                "json,created_at) VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(artifact_id) DO NOTHING",
                (
                    artifact.id,
                    artifact.mission_id,
                    artifact.task_id,
                    artifact.attempt_id,
                    artifact.path,
                    artifact.content_hash,
                    artifact.version,
                    canonical_json(artifact.to_json()),
                    artifact.created_at or self.now,
                ),
            )

    def update_artifact_verification(self, artifact_id: str, status: str) -> None:
        """P3.1 fix F-ORCH-3: an artifact follows its result's verdict — VERIFIED when the
        result is accepted, REJECTED when it failed.  The only field of an artifact row that
        changes after it was recorded (``upsert_artifact`` never rewrites a row).

        A missing row means the library is damaged: ``record_result`` writes every artifact
        in the same transaction, so it fails loudly rather than carrying on (review P2-5).
        """

        if status not in ARTIFACT_VERIFICATION_STATES:
            raise StoreError(f"unknown artifact verification status {status!r}")
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT json FROM artifacts WHERE artifact_id = ?", (artifact_id,)
            ).fetchone()
            if row is None:
                raise StoreError(f"artifact {artifact_id} is not recorded")
            data = json.loads(row[0])
            data["verification_status"] = status
            connection.execute(
                "UPDATE artifacts SET json = ? WHERE artifact_id = ?",
                (canonical_json(data), artifact_id),
            )

    # --------------------------------------------------------------- workspaces (P3.2 D4)
    def register_workspace(
        self,
        workspace_id: str,
        *,
        kind: str,
        mission_id: str,
        attempt_id: str,
        base_snapshot: str,
        state: str,
        detail: Mapping[str, Any],
    ) -> None:
        """Register (or re-register) one workspace directory: its identity and state."""

        if state not in WORKSPACE_STATES:
            raise StoreError(f"unknown workspace state {state!r}")
        now = self.now
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO workspaces(workspace_id,kind,mission_id,attempt_id,base_snapshot,"
                "state,json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(workspace_id) DO UPDATE SET base_snapshot=excluded.base_snapshot,"
                "state=excluded.state,json=excluded.json,updated_at=excluded.updated_at",
                (
                    workspace_id,
                    kind,
                    mission_id,
                    attempt_id,
                    base_snapshot,
                    state,
                    canonical_json(dict(detail)),
                    now,
                    now,
                ),
            )

    def set_workspace_state(self, workspace_id: str, state: str) -> None:
        if state not in WORKSPACE_STATES:
            raise StoreError(f"unknown workspace state {state!r}")
        with self.transaction() as connection:
            updated = connection.execute(
                "UPDATE workspaces SET state = ?, updated_at = ? WHERE workspace_id = ?",
                (state, self.now, workspace_id),
            ).rowcount
            if not updated:
                raise StoreError(f"workspace {workspace_id} is not registered")

    def get_workspace(self, workspace_id: str) -> dict[str, Any] | None:
        row = self._connection.execute(
            f"SELECT {_WORKSPACE_COLUMNS} FROM workspaces WHERE workspace_id = ?", (workspace_id,)
        ).fetchone()
        return None if row is None else _workspace_row(row)

    def list_workspaces(self, mission_id: str | None = None) -> list[dict[str, Any]]:
        if mission_id is None:
            rows = self._connection.execute(
                f"SELECT {_WORKSPACE_COLUMNS} FROM workspaces ORDER BY created_at, workspace_id"
            ).fetchall()
        else:
            rows = self._connection.execute(
                f"SELECT {_WORKSPACE_COLUMNS} FROM workspaces WHERE mission_id = ? "
                "ORDER BY created_at, workspace_id",
                (mission_id,),
            ).fetchall()
        return [_workspace_row(row) for row in rows]

    def list_all_artifacts(self) -> list[Artifact]:
        rows = self._connection.execute(
            "SELECT json FROM artifacts ORDER BY created_at, artifact_id"
        ).fetchall()
        return [Artifact.from_json(_loads(row[0])) for row in rows]

    def update_artifact_storage(self, changes: Sequence[tuple[str, str]]) -> None:
        """P3.2 D3 migration: point each artifact at its content-addressed file, or at
        ``""`` when its bytes were lost or changed before the upgrade (unavailable)."""

        with self.transaction() as connection:
            for artifact_id, storage_uri in changes:
                row = connection.execute(
                    "SELECT json FROM artifacts WHERE artifact_id = ?", (artifact_id,)
                ).fetchone()
                if row is None:
                    raise StoreError(f"artifact {artifact_id} is not recorded")
                data = json.loads(row[0])
                data["storage_uri"] = storage_uri
                connection.execute(
                    "UPDATE artifacts SET json = ? WHERE artifact_id = ?",
                    (canonical_json(data), artifact_id),
                )

    def get_artifact(self, artifact_id: str) -> Artifact | None:
        row = self._connection.execute(
            "SELECT json FROM artifacts WHERE artifact_id = ?", (artifact_id,)
        ).fetchone()
        return None if row is None else Artifact.from_json(_loads(row[0]))

    def list_artifacts(self, attempt_id: str) -> list[Artifact]:
        rows = self._connection.execute(
            "SELECT json FROM artifacts WHERE attempt_id = ? ORDER BY path, version", (attempt_id,)
        ).fetchall()
        return [Artifact.from_json(_loads(row[0])) for row in rows]

    def list_mission_artifacts(self, mission_id: str) -> list[Artifact]:
        """Every artifact of the Mission (version lineage is per (mission, path), D3-8')."""

        rows = self._connection.execute(
            "SELECT json FROM artifacts WHERE mission_id = ? ORDER BY path, version, artifact_id",
            (mission_id,),
        ).fetchall()
        return [Artifact.from_json(_loads(row[0])) for row in rows]

    # --------------------------------------------------------------- receipts
    def get_receipt(self, commit_id: str) -> Mapping[str, Any] | None:
        row = self._connection.execute(
            "SELECT receipt_json FROM commit_receipts WHERE commit_id = ?", (commit_id,)
        ).fetchone()
        return None if row is None else _loads(row[0])

    def insert_receipt(
        self,
        *,
        commit_id: str,
        kind: str,
        subject_id: str,
        base_version: int | None,
        proposal_hash: str,
        receipt: Mapping[str, Any],
    ) -> None:
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO commit_receipts(commit_id,kind,subject_id,base_version,proposal_hash,"
                "receipt_json,applied_at) VALUES (?,?,?,?,?,?,?)",
                (
                    commit_id,
                    kind,
                    subject_id,
                    base_version,
                    proposal_hash,
                    canonical_json(dict(receipt)),
                    self.now,
                ),
            )

    # ------------------------------------------------------------------ util
    def _cas(
        self,
        table: str,
        key_column: str,
        key: str,
        expected_version: int,
        values: Mapping[str, Any],
    ) -> None:
        assignments = ", ".join(f"{column} = ?" for column in values)
        with self.transaction() as connection:
            cursor = connection.execute(
                f"UPDATE {table} SET {assignments}, updated_at = ? WHERE {key_column} = ? AND version = ?",
                (*values.values(), self.now, key, expected_version),
            )
            if cursor.rowcount != 1:
                raise StoreConflict(f"{table}:{key} is not at version {expected_version}")

    # ------------------------------------------------------------ step 7 waiting view
    def waiting_on(self, mission_id: str) -> list[dict[str, Any]]:
        """D7-7': what an ACTIVE Mission waits on a person or a reconciliation for — a
        derived view, not a Mission state (ORCH §13 keeps the Mission state set)."""

        waiting: list[dict[str, Any]] = [
            {
                "kind": str(request["kind"]),
                "request_id": request["request_id"],
                "subject": request["subject_key"],
                "since": request.get("created_at"),
            }
            for request in self.list_approvals(mission_id, "PENDING")
        ]
        waiting.extend(
            {
                "kind": "reconciliation",
                "action_key": action["action_key"],
                "needs_human": bool(action.get("needs_human")),
                "since": action.get("handed_off_at"),
            }
            for action in self.list_actions(mission_id, "UNKNOWN")
        )
        waiting.extend(  # review P2-6: a hand-off whose outcome is not in yet (maybe a crash)
            {
                "kind": "handoff",
                "action_key": action["action_key"],
                "owner": action.get("owner"),
                "lease_expires_at": action.get("lease_expires_at"),
            }
            for action in self.list_actions(mission_id, "HANDED_OFF")
        )
        return waiting

    def human_wait_seconds(self, mission_id: str, now: float) -> float:
        """D7-7': the union of the spans during which a request of this Mission waited for
        a person (created → closed, or → now while still open)."""

        spans = sorted(
            (
                float(request.get("created_at") or 0.0),
                now
                if request["state"] == "PENDING"
                else float(request.get("closed_at") or request.get("created_at") or 0.0),
            )
            for request in self.list_approvals(mission_id)
        )
        total = 0.0
        current: tuple[float, float] | None = None
        for start, end in spans:
            if current is not None and start <= current[1]:
                current = (current[0], max(current[1], end))
                continue
            if current is not None:
                total += current[1] - current[0]
            current = (start, end)
        if current is not None:
            total += current[1] - current[0]
        return max(total, 0.0)

    def snapshot(self, mission_id: str) -> dict[str, Any]:
        """Everything about one Mission, for ``final_state.json`` and CLI ``get``."""

        mission = self.get_mission(mission_id)
        if mission is None:
            raise StoreError(f"unknown mission {mission_id}")
        tasks = self.list_tasks(mission_id)
        attempts = [attempt for task in tasks for attempt in self.list_attempts(task.id)]
        results = [self.find_result_for_attempt(attempt.id) for attempt in attempts]
        return {
            "mission": mission.to_json(),
            "tasks": [task.to_json() for task in tasks],
            "attempts": [attempt.to_json() for attempt in attempts],
            "results": [
                {**stored.to_json(), "verifications": self.list_verifications(stored.envelope.id)}
                for stored in results
                if stored is not None
            ],
            "claims": [claim.to_json() for claim in self.list_mission_claims(mission_id)],
            "knowledge": [record.to_json() for record in self.list_knowledge(mission_id)],
            "conflicts": self.list_conflicts(mission_id),
            "summaries": self.list_summaries(mission_id),
            "graph_changes": self.list_graph_changes(mission_id),
            "artifacts": [
                artifact.to_json()
                for attempt in attempts
                for artifact in self.list_artifacts(attempt.id)
            ],
            "intents": [
                intent.to_json()
                for attempt in attempts
                for intent in [self.get_intent_for_subject(attempt.id)]
                if intent is not None
            ],
            # step 7 (D7-11); an older library read by replay has no such tables (D8-1')
            "actions": self.list_actions(mission_id) if self.has_table("actions") else [],
            "approvals": self.list_approvals(mission_id) if self.has_table("approvals") else [],
            "human_overrides": self.list_overrides(mission_id)
            if self.has_table("human_overrides")
            else [],
            "waiting_on": self.waiting_on(mission_id) if self.has_table("approvals") else [],
            "mission_policy": self.get_mission_policy(mission_id),  # step 9 (plan D9-3')
            "mission_domain": self.get_mission_domain(mission_id),  # P3.3 (plan v3 D1/D9)
            "event_count": self.count_events(mission_id),
        }


def _event_from_row(row: sqlite3.Row) -> Event:
    return Event(
        id=row["event_id"],
        type=row["type"],
        trace_id=row["trace_id"],
        mission_id=row["mission_id"],
        task_id=row["task_id"],
        attempt_id=row["attempt_id"],
        actor_type=row["actor_type"],
        actor_id=row["actor_id"],
        payload=_loads(row["payload_json"]),
        idempotency_key=row["idempotency_key"],
        created_at=row["created_at"],
        schema_version=row["schema_version"],
        seq=row["seq"],
    )


def _intent_from_row(row: sqlite3.Row) -> DispatchIntent:
    return DispatchIntent(
        intent_id=row["intent_id"],
        kind=row["kind"],
        subject_id=row["subject_id"],
        mission_id=row["mission_id"],
        state=row["state"],
        version=row["version"],
        creation_key=row["creation_key"],
        input_id=row["input_id"],
        input_hash=row["input_hash"],
        config=_loads(row["config_json"]),
        expected_turn_id=row["expected_turn_id"],
        agent_id=row["agent_id"],
        receipt=None if row["receipt_json"] is None else _loads(row["receipt_json"]),
        lease_owner=row["lease_owner"],
        lease_expires_at=row["lease_expires_at"],
        replays=row["replays"],
        created_at=row["created_at"],
    )


def _stored_result(data: Mapping[str, Any]) -> StoredResult:
    try:
        return StoredResult(
            envelope=ResultEnvelope.from_json(data["envelope"], strict=False),
            turn_id=data["turn_id"],
            verification_state=data["verification_state"],
            verdict=data.get("verdict"),
            received_at=data["received_at"],
            artifacts=tuple(data.get("artifacts", ())),
            usage_refs=tuple(data.get("usage_refs", ())),
        )
    except (KeyError, ContractError) as error:
        raise StoreError(f"corrupt stored result: {error}") from error


def proposal_hash(proposal: object) -> str:
    return sha256_hex(proposal)


__all__ = (
    "INTENT_STATES",
    "VERIFICATION_STATES",
    "DispatchIntent",
    "InjectedCrash",
    "SchemaIncompatible",
    "Store",
    "StoreBusy",
    "StoreConflict",
    "StoreError",
    "StoredResult",
    "proposal_hash",
)
