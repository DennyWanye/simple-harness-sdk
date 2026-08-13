# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Atomic SQLite command implementation for root execution lifecycle."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from typing import Mapping

from simple_harness.contracts import JsonValue, canonical_json, freeze_json
from simple_harness.execution.uow import (
    AdmissionRecord,
    AdmissionState,
    ContinuationRecord,
    ContinuationState,
    DecisionRecord,
    DecisionState,
    FaultHook,
    RunRecord,
    RunState,
    UnitOfWorkConflict,
    UnitOfWorkNotFound,
)

from .database import Database


def _required(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _time(value: object, name: str = "now") -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
        or value < 0
    ):
        raise ValueError(f"{name} must be finite and non-negative")
    return float(value)


def _object_json(value: Mapping[str, JsonValue], name: str) -> str:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be a JSON object")
    return canonical_json(value)


def _fault(hook: FaultHook | None, point: str) -> None:
    if hook is not None:
        hook(point)


class SqliteExecutionUnitOfWork:
    __slots__ = ("database",)

    def __init__(self, database: Database) -> None:
        self.database = database

    def create_with_start_snapshot(
        self,
        *,
        execution_session_id: str,
        run_id: str,
        request_id: str,
        profile_key: str,
        driver_kind: str,
        snapshot: Mapping[str, JsonValue],
        event_id: str,
        now: float,
        fault: FaultHook | None = None,
    ) -> RunRecord:
        execution_session_id = _required(execution_session_id, "execution_session_id")
        run_id = _required(run_id, "run_id")
        request_id = _required(request_id, "request_id")
        profile_key = _required(profile_key, "profile_key")
        driver_kind = _required(driver_kind, "driver_kind")
        event_id = _required(event_id, "event_id")
        now = _time(now)
        snapshot_json = _object_json(snapshot, "snapshot")
        snapshot_hash = hashlib.sha256(snapshot_json.encode("utf-8")).hexdigest()
        existing = self._run_by_request(execution_session_id, request_id)
        if existing is not None:
            self._verify_existing_start(
                existing,
                run_id=run_id,
                profile_key=profile_key,
                driver_kind=driver_kind,
                snapshot_hash=snapshot_hash,
            )
            return existing
        with self.database.transaction() as connection:
            _fault(fault, "root_start.session.before_write")
            connection.execute(
                "INSERT OR IGNORE INTO execution_sessions(session_id, created_at) VALUES (?, ?)",
                (execution_session_id, now),
            )
            _fault(fault, "root_start.session.after_write")
            _fault(fault, "root_start.run.before_write")
            connection.execute(
                """
                INSERT INTO runs(
                    run_id, execution_session_id, request_id, root_run_id,
                    parent_run_id, profile_key, driver_kind, state, version,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, NULL, ?, ?, 'created', 0, ?, ?)
                """,
                (
                    run_id,
                    execution_session_id,
                    request_id,
                    run_id,
                    profile_key,
                    driver_kind,
                    now,
                    now,
                ),
            )
            _fault(fault, "root_start.run.after_write")
            _fault(fault, "root_start.snapshot.before_write")
            connection.execute(
                """
                INSERT INTO run_start_snapshots(run_id, snapshot_json, snapshot_hash, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (run_id, snapshot_json, snapshot_hash, now),
            )
            _fault(fault, "root_start.snapshot.after_write")
            _fault(fault, "root_start.event.before_write")
            self._insert_event(
                connection,
                event_id=event_id,
                run_id=run_id,
                kind="run.created",
                payload={"profile_key": profile_key, "driver_kind": driver_kind},
                now=now,
            )
            _fault(fault, "root_start.event.after_write")
        _fault(fault, "root_start.after_commit")
        record = self.read_run(run_id)
        assert record is not None
        return record

    def start_admission(
        self,
        *,
        admission_id: str,
        run_id: str,
        prompt: Mapping[str, JsonValue],
        expires_at: float | None,
        event_id: str,
        now: float,
        fault: FaultHook | None = None,
    ) -> AdmissionRecord:
        admission_id = _required(admission_id, "admission_id")
        run_id = _required(run_id, "run_id")
        event_id = _required(event_id, "event_id")
        now = _time(now)
        if expires_at is not None:
            expires_at = _time(expires_at, "expires_at")
        prompt_json = _object_json(prompt, "prompt")
        existing = self.read_admission(admission_id)
        if existing is not None:
            if (
                existing.run_id != run_id
                or canonical_json(json.loads(prompt_json))
                != canonical_json(_thaw(existing.prompt))
                or existing.expires_at != expires_at
            ):
                raise UnitOfWorkConflict("admission identity reused with different intent")
            return existing
        with self.database.transaction() as connection:
            _fault(fault, "admission_start.run.before_write")
            changed = connection.execute(
                """
                UPDATE runs SET state = 'admission_pending', version = version + 1, updated_at = ?
                WHERE run_id = ? AND state = 'created'
                """,
                (now, run_id),
            ).rowcount
            if changed != 1:
                raise UnitOfWorkConflict("run is not eligible for admission")
            _fault(fault, "admission_start.run.after_write")
            _fault(fault, "admission_start.admission.before_write")
            connection.execute(
                """
                INSERT INTO run_admissions(
                    admission_id, run_id, state, prompt_json, response_json,
                    expires_at, version, created_at, resolved_at
                ) VALUES (?, ?, 'pending', ?, NULL, ?, 0, ?, NULL)
                """,
                (admission_id, run_id, prompt_json, expires_at, now),
            )
            _fault(fault, "admission_start.admission.after_write")
            _fault(fault, "admission_start.event.before_write")
            self._insert_event(
                connection,
                event_id=event_id,
                run_id=run_id,
                kind="admission.started",
                payload={"admission_id": admission_id},
                now=now,
            )
            _fault(fault, "admission_start.event.after_write")
        _fault(fault, "admission_start.after_commit")
        result = self.read_admission(admission_id)
        assert result is not None
        return result

    def resolve_admission(
        self,
        *,
        admission_id: str,
        state: AdmissionState,
        response: Mapping[str, JsonValue],
        event_id: str,
        now: float,
        fault: FaultHook | None = None,
    ) -> AdmissionRecord:
        admission_id = _required(admission_id, "admission_id")
        event_id = _required(event_id, "event_id")
        now = _time(now)
        state = AdmissionState(state)
        if state is AdmissionState.PENDING:
            raise ValueError("resolve_admission requires a terminal admission state")
        response_json = _object_json(response, "response")
        existing = self.read_admission(admission_id)
        if existing is None:
            raise UnitOfWorkNotFound(admission_id)
        if existing.state is not AdmissionState.PENDING:
            if existing.state is state and canonical_json(_thaw(existing.response)) == response_json:
                return existing
            raise UnitOfWorkConflict("admission already resolved differently")
        run_state = {
            AdmissionState.ALLOWED: RunState.QUEUED,
            AdmissionState.DENIED: RunState.FAILED,
            AdmissionState.EXPIRED: RunState.FAILED,
            AdmissionState.CANCELLED: RunState.CANCELLED,
        }[state]
        with self.database.transaction() as connection:
            _fault(fault, "admission_resolve.admission.before_write")
            changed = connection.execute(
                """
                UPDATE run_admissions
                SET state = ?, response_json = ?, version = version + 1, resolved_at = ?
                WHERE admission_id = ? AND state = 'pending' AND version = ?
                """,
                (state.value, response_json, now, admission_id, existing.version),
            ).rowcount
            if changed != 1:
                raise UnitOfWorkConflict("admission CAS failed")
            _fault(fault, "admission_resolve.admission.after_write")
            _fault(fault, "admission_resolve.run.before_write")
            changed = connection.execute(
                """
                UPDATE runs SET state = ?, version = version + 1, updated_at = ?
                WHERE run_id = ? AND state = 'admission_pending'
                """,
                (run_state.value, now, existing.run_id),
            ).rowcount
            if changed != 1:
                raise UnitOfWorkConflict("run admission state CAS failed")
            _fault(fault, "admission_resolve.run.after_write")
            _fault(fault, "admission_resolve.event.before_write")
            self._insert_event(
                connection,
                event_id=event_id,
                run_id=existing.run_id,
                kind=f"admission.{state.value}",
                payload={"admission_id": admission_id},
                now=now,
            )
            _fault(fault, "admission_resolve.event.after_write")
        _fault(fault, "admission_resolve.after_commit")
        result = self.read_admission(admission_id)
        assert result is not None
        return result

    def commit_decision(
        self,
        *,
        decision_id: str,
        run_id: str,
        kind: str,
        state: DecisionState,
        request: Mapping[str, JsonValue],
        response: Mapping[str, JsonValue] | None,
        event_id: str,
        now: float,
        fault: FaultHook | None = None,
    ) -> DecisionRecord:
        decision_id = _required(decision_id, "decision_id")
        run_id = _required(run_id, "run_id")
        kind = _required(kind, "kind")
        event_id = _required(event_id, "event_id")
        now = _time(now)
        state = DecisionState(state)
        request_json = _object_json(request, "request")
        response_json = None if response is None else _object_json(response, "response")
        if (state is DecisionState.OPEN) != (response is None):
            raise ValueError("open decision must omit response; resolved decision requires it")
        existing = self.read_decision(decision_id)
        if existing is not None:
            if (
                existing.run_id == run_id
                and existing.kind == kind
                and existing.state is state
                and canonical_json(_thaw(existing.request)) == request_json
                and (
                    (existing.response is None and response_json is None)
                    or canonical_json(_thaw(existing.response)) == response_json
                )
            ):
                return existing
            raise UnitOfWorkConflict("decision identity reused with different intent")
        run_state = {
            DecisionState.OPEN: RunState.WAITING,
            DecisionState.ALLOWED: RunState.RUNNING,
            DecisionState.DENIED: RunState.FAILED,
            DecisionState.EXPIRED: RunState.FAILED,
            DecisionState.CANCELLED: RunState.CANCELLED,
        }[state]
        with self.database.transaction() as connection:
            _fault(fault, "decision.decision.before_write")
            connection.execute(
                """
                INSERT INTO decisions(
                    decision_id, run_id, kind, state, request_json, response_json,
                    version, created_at, resolved_at
                ) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)
                """,
                (
                    decision_id,
                    run_id,
                    kind,
                    state.value,
                    request_json,
                    response_json,
                    now,
                    None if state is DecisionState.OPEN else now,
                ),
            )
            _fault(fault, "decision.decision.after_write")
            _fault(fault, "decision.run.before_write")
            changed = connection.execute(
                "UPDATE runs SET state = ?, version = version + 1, updated_at = ? WHERE run_id = ?",
                (run_state.value, now, run_id),
            ).rowcount
            if changed != 1:
                raise UnitOfWorkNotFound(run_id)
            _fault(fault, "decision.run.after_write")
            _fault(fault, "decision.event.before_write")
            self._insert_event(
                connection,
                event_id=event_id,
                run_id=run_id,
                kind=f"decision.{state.value}",
                payload={"decision_id": decision_id, "kind": kind},
                now=now,
            )
            _fault(fault, "decision.event.after_write")
        _fault(fault, "decision.after_commit")
        result = self.read_decision(decision_id)
        assert result is not None
        return result

    def enqueue_continuation(
        self,
        *,
        continuation_id: str,
        run_id: str,
        payload: Mapping[str, JsonValue],
        now: float,
        fault: FaultHook | None = None,
    ) -> ContinuationRecord:
        continuation_id = _required(continuation_id, "continuation_id")
        run_id = _required(run_id, "run_id")
        now = _time(now)
        payload_json = _object_json(payload, "payload")
        existing = self.read_continuation(continuation_id)
        if existing is not None:
            if existing.run_id == run_id and canonical_json(_thaw(existing.payload)) == payload_json:
                return existing
            raise UnitOfWorkConflict("continuation identity reused with different payload")
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(fifo_seq), 0) + 1 FROM continuations WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            sequence = int(row[0])
            _fault(fault, "continuation_enqueue.continuation.before_write")
            connection.execute(
                """
                INSERT INTO continuations(
                    continuation_id, run_id, fifo_seq, payload_json, state,
                    version, claimed_by, created_at, claimed_at, acked_at
                ) VALUES (?, ?, ?, ?, 'pending', 0, NULL, ?, NULL, NULL)
                """,
                (continuation_id, run_id, sequence, payload_json, now),
            )
            _fault(fault, "continuation_enqueue.continuation.after_write")
        _fault(fault, "continuation_enqueue.after_commit")
        result = self.read_continuation(continuation_id)
        assert result is not None
        return result

    def claim_continuation(
        self,
        *,
        run_id: str,
        owner_id: str,
        now: float,
        fault: FaultHook | None = None,
    ) -> ContinuationRecord | None:
        run_id = _required(run_id, "run_id")
        owner_id = _required(owner_id, "owner_id")
        now = _time(now)
        claimed_id: str | None = None
        with self.database.transaction() as connection:
            row = connection.execute(
                """
                SELECT continuation_id, version FROM continuations
                WHERE run_id = ? AND state = 'pending'
                ORDER BY fifo_seq ASC LIMIT 1
                """,
                (run_id,),
            ).fetchone()
            if row is not None:
                claimed_id = str(row[0])
                _fault(fault, "continuation_claim.continuation.before_write")
                changed = connection.execute(
                    """
                    UPDATE continuations
                    SET state = 'claimed', version = version + 1,
                        claimed_by = ?, claimed_at = ?
                    WHERE continuation_id = ? AND state = 'pending' AND version = ?
                    """,
                    (owner_id, now, claimed_id, int(row[1])),
                ).rowcount
                if changed != 1:
                    raise UnitOfWorkConflict("continuation claim CAS failed")
                _fault(fault, "continuation_claim.continuation.after_write")
        _fault(fault, "continuation_claim.after_commit")
        return None if claimed_id is None else self.read_continuation(claimed_id)

    def ack_continuation(
        self,
        *,
        continuation_id: str,
        owner_id: str,
        expected_version: int,
        now: float,
        fault: FaultHook | None = None,
    ) -> ContinuationRecord:
        continuation_id = _required(continuation_id, "continuation_id")
        owner_id = _required(owner_id, "owner_id")
        now = _time(now)
        existing = self.read_continuation(continuation_id)
        if existing is None:
            raise UnitOfWorkNotFound(continuation_id)
        if existing.state is ContinuationState.ACKED:
            if existing.claimed_by == owner_id and existing.version == expected_version + 1:
                return existing
            raise UnitOfWorkConflict("continuation already acked by another claim")
        with self.database.transaction() as connection:
            _fault(fault, "continuation_ack.continuation.before_write")
            changed = connection.execute(
                """
                UPDATE continuations
                SET state = 'acked', version = version + 1, acked_at = ?
                WHERE continuation_id = ? AND state = 'claimed'
                  AND claimed_by = ? AND version = ?
                """,
                (now, continuation_id, owner_id, expected_version),
            ).rowcount
            if changed != 1:
                raise UnitOfWorkConflict("continuation ack CAS failed")
            _fault(fault, "continuation_ack.continuation.after_write")
        _fault(fault, "continuation_ack.after_commit")
        result = self.read_continuation(continuation_id)
        assert result is not None
        return result

    def read_run(self, run_id: str) -> RunRecord | None:
        row = self.database.connection.execute(
            "SELECT * FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        return None if row is None else _run_record(row)

    def read_admission(self, admission_id: str) -> AdmissionRecord | None:
        row = self.database.connection.execute(
            "SELECT * FROM run_admissions WHERE admission_id = ?", (admission_id,)
        ).fetchone()
        return None if row is None else _admission_record(row)

    def read_decision(self, decision_id: str) -> DecisionRecord | None:
        row = self.database.connection.execute(
            "SELECT * FROM decisions WHERE decision_id = ?", (decision_id,)
        ).fetchone()
        return None if row is None else _decision_record(row)

    def read_continuation(self, continuation_id: str) -> ContinuationRecord | None:
        row = self.database.connection.execute(
            "SELECT * FROM continuations WHERE continuation_id = ?",
            (continuation_id,),
        ).fetchone()
        return None if row is None else _continuation_record(row)

    def _run_by_request(self, session_id: str, request_id: str) -> RunRecord | None:
        row = self.database.connection.execute(
            "SELECT * FROM runs WHERE execution_session_id = ? AND request_id = ?",
            (session_id, request_id),
        ).fetchone()
        return None if row is None else _run_record(row)

    def _verify_existing_start(
        self,
        record: RunRecord,
        *,
        run_id: str,
        profile_key: str,
        driver_kind: str,
        snapshot_hash: str,
    ) -> None:
        row = self.database.connection.execute(
            "SELECT snapshot_hash FROM run_start_snapshots WHERE run_id = ?",
            (record.run_id,),
        ).fetchone()
        if (
            record.run_id != run_id
            or record.profile_key != profile_key
            or record.driver_kind != driver_kind
            or row is None
            or row[0] != snapshot_hash
        ):
            raise UnitOfWorkConflict("request identity reused with different root intent")

    def _insert_event(
        self,
        connection: sqlite3.Connection,
        *,
        event_id: str,
        run_id: str,
        kind: str,
        payload: dict[str, JsonValue],
        now: float,
    ) -> None:
        sequence = int(
            connection.execute(
                "SELECT COALESCE(MAX(durable_seq), 0) + 1 FROM run_events WHERE run_id = ?",
                (run_id,),
            ).fetchone()[0]
        )
        connection.execute(
            """
            INSERT INTO run_events(event_id, run_id, durable_seq, kind, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (event_id, run_id, sequence, kind, canonical_json(payload), now),
        )


def _thaw(value: object) -> dict[str, JsonValue]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return {str(key): _thaw_json(item) for key, item in value.items()}
    raise TypeError("expected JSON object")


def _thaw_json(value: object) -> JsonValue:
    if isinstance(value, Mapping):
        return {str(key): _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    raise TypeError(type(value).__name__)


def _run_record(row: sqlite3.Row) -> RunRecord:
    return RunRecord(
        run_id=str(row["run_id"]),
        execution_session_id=str(row["execution_session_id"]),
        request_id=str(row["request_id"]),
        root_run_id=str(row["root_run_id"]),
        parent_run_id=None if row["parent_run_id"] is None else str(row["parent_run_id"]),
        profile_key=str(row["profile_key"]),
        driver_kind=str(row["driver_kind"]),
        state=RunState(row["state"]),
        version=int(row["version"]),
    )


def _admission_record(row: sqlite3.Row) -> AdmissionRecord:
    prompt = freeze_json(json.loads(str(row["prompt_json"])))
    response = (
        None
        if row["response_json"] is None
        else freeze_json(json.loads(str(row["response_json"])))
    )
    return AdmissionRecord(
        admission_id=str(row["admission_id"]),
        run_id=str(row["run_id"]),
        state=AdmissionState(row["state"]),
        prompt=prompt,
        response=response,
        expires_at=None if row["expires_at"] is None else float(row["expires_at"]),
        version=int(row["version"]),
    )


def _decision_record(row: sqlite3.Row) -> DecisionRecord:
    return DecisionRecord(
        decision_id=str(row["decision_id"]),
        run_id=str(row["run_id"]),
        kind=str(row["kind"]),
        state=DecisionState(row["state"]),
        request=freeze_json(json.loads(str(row["request_json"]))),
        response=(
            None
            if row["response_json"] is None
            else freeze_json(json.loads(str(row["response_json"])))
        ),
        version=int(row["version"]),
    )


def _continuation_record(row: sqlite3.Row) -> ContinuationRecord:
    return ContinuationRecord(
        continuation_id=str(row["continuation_id"]),
        run_id=str(row["run_id"]),
        fifo_seq=int(row["fifo_seq"]),
        payload=freeze_json(json.loads(str(row["payload_json"]))),
        state=ContinuationState(row["state"]),
        version=int(row["version"]),
        claimed_by=None if row["claimed_by"] is None else str(row["claimed_by"]),
    )


__all__ = ("SqliteExecutionUnitOfWork",)
