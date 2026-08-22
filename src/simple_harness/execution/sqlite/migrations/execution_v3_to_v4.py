# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Explicit, backup-first execution schema v3 to v4 migration.

The normal :class:`Database` loader intentionally does not call this module.
The product coordinator must close the Runtime, provide an exact legacy
identity map and retain the backup until the two-database cutover completes.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from importlib.resources import files
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from simple_harness.contracts import JsonValue, canonical_json
from simple_harness.runtime.agent_memory import AgentIdentity, CommittedTurn, MemoryScopeRef
from simple_harness.runtime.conversation_memory import (
    ConversationMemoryIntent,
    ConversationMemoryRole,
)


class ExecutionMigrationError(RuntimeError):
    """Stable offline migration failure without database content in its message."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class LegacyDisposition(StrEnum):
    KEEP_COMPLETED_PAIR = "keep_completed_pair"
    SUPPRESS_TENTATIVE = "suppress_tentative"
    SUPPRESS_TERMINAL = "suppress_terminal"
    DEFERRED_TURN = "deferred_turn"


@dataclass(frozen=True, slots=True)
class LegacyIdentityBinding:
    user_id: str
    session_id: str
    identity: AgentIdentity

    def __post_init__(self) -> None:
        _required(self.user_id, "user_id")
        _required(self.session_id, "session_id")
        if not isinstance(self.identity, AgentIdentity):
            raise TypeError("identity must use AgentIdentity")


@dataclass(frozen=True, slots=True)
class LegacyIdentityMap:
    bindings: tuple[LegacyIdentityBinding, ...]
    digest: str = field(init=False)

    def __post_init__(self) -> None:
        bindings = tuple(self.bindings)
        if not bindings:
            raise ValueError("legacy identity map must not be empty")
        keys = [(item.user_id, item.session_id) for item in bindings]
        if len(keys) != len(set(keys)):
            raise ValueError("legacy identity map is ambiguous")
        target_sessions = [item.identity.session_id for item in bindings]
        if len(target_sessions) != len(set(target_sessions)):
            raise ValueError("legacy identity map target sessions are ambiguous")
        ordered = tuple(sorted(bindings, key=lambda item: (item.session_id, item.user_id)))
        object.__setattr__(self, "bindings", ordered)
        object.__setattr__(
            self,
            "digest",
            _sha256(
                canonical_json(
                    {
                        "protocol": "simple-harness/legacy-identity-map/v1",
                        "bindings": [
                            {
                                "user_id": item.user_id,
                                "session_id": item.session_id,
                                "identity": item.identity.to_json(),
                            }
                            for item in ordered
                        ],
                    }
                )
            ),
        )

    @classmethod
    def from_bindings(cls, bindings: Iterable[LegacyIdentityBinding]) -> LegacyIdentityMap:
        return cls(tuple(bindings))

    def by_legacy_key(self) -> dict[tuple[str, str], AgentIdentity]:
        return {(item.user_id, item.session_id): item.identity for item in self.bindings}


@dataclass(frozen=True, slots=True)
class MigrationManifestEntry:
    source_event_id: str
    source_key: str
    disposition: LegacyDisposition
    payload_hash: str
    run_id: str
    turn_id: str | None
    causal_terminal_event_id: str | None
    causal_continuation_id: str | None
    causal_claim_epoch: int | None
    canonical_turn: Mapping[str, JsonValue] | None
    canonical_turn_hash: str | None

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "source_event_id": self.source_event_id,
            "source_key": self.source_key,
            "disposition": self.disposition.value,
            "payload_hash": self.payload_hash,
            "run_id": self.run_id,
            "turn_id": self.turn_id,
            "causal_terminal_event_id": self.causal_terminal_event_id,
            "causal_continuation_id": self.causal_continuation_id,
            "causal_claim_epoch": self.causal_claim_epoch,
            "canonical_turn": None if self.canonical_turn is None else dict(self.canonical_turn),
            "canonical_turn_hash": self.canonical_turn_hash,
        }


@dataclass(frozen=True, slots=True)
class ExecutionMigrationManifest:
    identity_map_digest: str
    source_schema: int
    target_schema: int
    source_database_hash: str
    entries: tuple[MigrationManifestEntry, ...]
    digest: str = field(init=False)

    def __post_init__(self) -> None:
        ordered = tuple(sorted(self.entries, key=lambda item: item.source_event_id))
        if len({item.source_event_id for item in ordered}) != len(ordered):
            raise ValueError("migration manifest contains duplicate source events")
        object.__setattr__(self, "entries", ordered)
        object.__setattr__(self, "digest", _sha256(canonical_json(self._payload())))

    def _payload(self) -> dict[str, JsonValue]:
        return {
            "protocol": "simple-harness/execution-migration-manifest/v1",
            "identity_map_digest": self.identity_map_digest,
            "source_schema": self.source_schema,
            "target_schema": self.target_schema,
            "source_database_hash": self.source_database_hash,
            "entries": [item.to_json() for item in self.entries],
        }

    def to_json(self) -> dict[str, JsonValue]:
        return {**self._payload(), "digest": self.digest}

    @classmethod
    def from_json(cls, value: Mapping[str, JsonValue]) -> ExecutionMigrationManifest:
        if value.get("protocol") != "simple-harness/execution-migration-manifest/v1":
            raise ValueError("execution migration manifest protocol is invalid")
        raw_entries = value.get("entries")
        if not isinstance(raw_entries, list):
            raise TypeError("execution migration manifest entries must be an array")
        entries: list[MigrationManifestEntry] = []
        for raw in raw_entries:
            if not isinstance(raw, Mapping):
                raise TypeError("execution migration manifest entry must be an object")
            raw_turn = raw.get("canonical_turn")
            if raw_turn is not None and not isinstance(raw_turn, Mapping):
                raise TypeError("canonical_turn must be an object or null")
            claim_epoch = raw.get("causal_claim_epoch")
            if claim_epoch is not None and (
                isinstance(claim_epoch, bool) or not isinstance(claim_epoch, int)
            ):
                raise TypeError("causal_claim_epoch must be an integer or null")
            entries.append(
                MigrationManifestEntry(
                    _required(raw.get("source_event_id"), "source_event_id"),
                    _required(raw.get("source_key"), "source_key"),
                    LegacyDisposition(_required(raw.get("disposition"), "disposition")),
                    _digest(raw.get("payload_hash"), "payload_hash"),
                    _required(raw.get("run_id"), "run_id"),
                    _optional_required(raw.get("turn_id"), "turn_id"),
                    _optional_required(
                        raw.get("causal_terminal_event_id"), "causal_terminal_event_id"
                    ),
                    _optional_required(raw.get("causal_continuation_id"), "causal_continuation_id"),
                    claim_epoch,
                    cast(Mapping[str, JsonValue] | None, raw_turn),
                    _optional_digest(raw.get("canonical_turn_hash"), "canonical_turn_hash"),
                )
            )
        source_schema = value.get("source_schema")
        target_schema = value.get("target_schema")
        if source_schema != 3 or target_schema != 4:
            raise ValueError("execution migration manifest schema identity is invalid")
        result = cls(
            _digest(value.get("identity_map_digest"), "identity_map_digest"),
            3,
            4,
            _digest(value.get("source_database_hash"), "source_database_hash"),
            tuple(entries),
        )
        if value.get("digest") != result.digest:
            raise ValueError("execution migration manifest digest differs")
        return result


@dataclass(frozen=True, slots=True)
class _ResolvedRun:
    entries: tuple[MigrationManifestEntry, ...]
    committed_turn: CommittedTurn | None
    cursor_entry: MigrationManifestEntry | None
    cursor_text: str | None
    cursor_started_at: float | None


FaultHook = Callable[[str], None]


def migrate_execution_v3_to_v4(
    database_path: str | Path,
    *,
    backup_path: str | Path,
    identity_map: LegacyIdentityMap,
    fault: FaultHook | None = None,
) -> ExecutionMigrationManifest:
    """Replace one closed v3 database with a validated v4 database.

    ``backup_path`` must not already exist. Any failure after replacement
    restores the validated v3 backup before this function returns an error.
    """

    if not isinstance(identity_map, LegacyIdentityMap):
        raise TypeError("identity_map must use LegacyIdentityMap")
    source_path = Path(database_path).expanduser().resolve()
    backup = Path(backup_path).expanduser().resolve()
    if source_path == backup:
        raise ValueError("backup_path must differ from database_path")
    if not source_path.is_file():
        raise ExecutionMigrationError("execution_migration_source_missing")
    if backup.exists():
        raise ExecutionMigrationError("execution_migration_backup_exists")
    if backup.parent != source_path.parent:
        raise ValueError("backup_path must share the database directory")
    temp = source_path.with_name(f".{source_path.name}.v4-{uuid4().hex}.tmp")
    restore = source_path.with_name(f".{source_path.name}.restore-{uuid4().hex}.tmp")
    source_hash = ""
    replaced = False
    source: sqlite3.Connection | None = None
    try:
        source = sqlite3.connect(source_path, timeout=0.0, isolation_level=None)
        source.row_factory = sqlite3.Row
        source.execute("PRAGMA foreign_keys = ON")
        source.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        source_hash = _file_hash(source_path)
        try:
            source.execute("BEGIN EXCLUSIVE")
        except sqlite3.OperationalError as error:
            raise ExecutionMigrationError("execution_migration_runtime_not_closed") from error
        _validate_v3(source)
        _validate_identity_map(source, identity_map)
        if tuple(source.execute("PRAGMA integrity_check").fetchone()) != ("ok",):
            raise ExecutionMigrationError("execution_migration_source_integrity")
        if source.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise ExecutionMigrationError("execution_migration_source_foreign_key")
        shutil.copy2(source_path, backup)
        os.chmod(backup, 0o600)
        if _file_hash(backup) != source_hash:
            raise ExecutionMigrationError("execution_migration_backup_hash")
        _emit_fault(fault, "execution_migration.backup.after_copy")
        manifest = _build_manifest(source, identity_map, source_hash)
        _build_v4(source, temp, identity_map, manifest, fault=fault)
        _emit_fault(fault, "execution_migration.before_replace")
        source.commit()
        source.close()
        source = None
        os.replace(temp, source_path)
        replaced = True
        _emit_fault(fault, "execution_migration.after_replace")
        _validate_replaced_v4(source_path, manifest)
        _emit_fault(fault, "execution_migration.after_validate")
        return manifest
    except BaseException:
        if source is not None:
            if source.in_transaction:
                source.rollback()
            source.close()
        if replaced:
            shutil.copy2(backup, restore)
            os.chmod(restore, 0o600)
            os.replace(restore, source_path)
        raise
    finally:
        for disposable in (temp, restore):
            try:
                disposable.unlink()
            except FileNotFoundError:
                pass


def _validate_v3(connection: sqlite3.Connection) -> None:
    rows = connection.execute(
        "SELECT version,name,checksum FROM sdk_schema_migrations ORDER BY version"
    ).fetchall()
    legacy_sql = (
        files("simple_harness.execution.sqlite.migrations")
        .joinpath("0003_fresh.sql")
        .read_text(encoding="utf-8")
    )
    expected = (3, "0003_fresh", _sha256(legacy_sql))
    if len(rows) != 1 or tuple(rows[0]) != expected:
        raise ExecutionMigrationError("execution_migration_requires_exact_v3")


def _validate_identity_map(connection: sqlite3.Connection, identity_map: LegacyIdentityMap) -> None:
    actual = {
        (str(row[0]), str(row[1]))
        for row in connection.execute(
            "SELECT user_id,session_id FROM execution_sessions ORDER BY session_id"
        )
    }
    supplied = set(identity_map.by_legacy_key())
    if actual != supplied:
        raise ExecutionMigrationError("execution_migration_identity_map_incomplete")


def _build_manifest(
    connection: sqlite3.Connection,
    identity_map: LegacyIdentityMap,
    source_hash: str,
) -> ExecutionMigrationManifest:
    identities = identity_map.by_legacy_key()
    all_entries: list[MigrationManifestEntry] = []
    run_rows = connection.execute(
        "SELECT r.run_id,r.state,r.execution_session_id,s.user_id "
        "FROM runs AS r JOIN execution_sessions AS s "
        "ON s.session_id=r.execution_session_id ORDER BY r.run_id"
    ).fetchall()
    for run in run_rows:
        identity = identities[(str(run["user_id"]), str(run["execution_session_id"]))]
        resolved = _resolve_run(connection, run, identity)
        all_entries.extend(resolved.entries)
    source_count = int(connection.execute("SELECT COUNT(*) FROM memory_outbox").fetchone()[0])
    if len(all_entries) != source_count:
        raise ExecutionMigrationError("execution_migration_manifest_incomplete")
    return ExecutionMigrationManifest(
        identity_map.digest,
        3,
        4,
        source_hash,
        tuple(all_entries),
    )


def _resolve_run(
    connection: sqlite3.Connection,
    run: sqlite3.Row,
    identity: AgentIdentity,
) -> _ResolvedRun:
    run_id = str(run["run_id"])
    state = str(run["state"])
    rows = connection.execute(
        "SELECT * FROM memory_outbox WHERE run_id=? ORDER BY source_event_id", (run_id,)
    ).fetchall()
    for row in rows:
        _validate_legacy_intent(row)
        if str(row["session_id"]) != str(run["execution_session_id"]) or str(row["user_id"]) != str(
            run["user_id"]
        ):
            raise ExecutionMigrationError("execution_migration_legacy_identity_mismatch")
    if not rows:
        return _ResolvedRun((), None, None, None, None)
    if state in {"failed", "cancelled"}:
        return _ResolvedRun(
            tuple(_entry(row, LegacyDisposition.SUPPRESS_TERMINAL) for row in rows),
            None,
            None,
            None,
            None,
        )
    users = [row for row in rows if str(row["role"]) == "user"]
    assistants = [row for row in rows if str(row["role"]) == "assistant"]
    if state != "completed":
        if assistants:
            raise ExecutionMigrationError("execution_migration_nonterminal_assistant")
        ordered = _ordered_users(connection, run_id, users)
        if not ordered:
            return _ResolvedRun((), None, None, None, None)
        latest = ordered[-1]
        if latest["memory_text"] is None:
            raise ExecutionMigrationError("execution_migration_deferred_non_text")
        turn_id = _legacy_turn_id(connection, run_id, latest)
        entries = [_entry(row, LegacyDisposition.SUPPRESS_TENTATIVE) for row in ordered[:-1]]
        deferred = _entry(
            latest,
            LegacyDisposition.DEFERRED_TURN,
            turn_id=turn_id,
            causal_continuation_id=_optional_str(latest["continuation_id"]),
        )
        entries.append(deferred)
        return _ResolvedRun(
            tuple(entries),
            None,
            deferred,
            str(latest["memory_text"]),
            float(latest["created_at"]),
        )
    if len(assistants) != 1 or not users:
        raise ExecutionMigrationError("execution_migration_completed_pair_missing")
    assistant = assistants[0]
    terminal_event, continuation_id, claim_epoch = _resolve_terminal_cause(
        connection, run_id, assistant
    )
    eligible = [row for row in users if _optional_str(row["continuation_id"]) == continuation_id]
    if len(eligible) != 1:
        raise ExecutionMigrationError("execution_migration_terminal_user_ambiguous")
    user = eligible[0]
    if user["memory_text"] is None or assistant["memory_text"] is None:
        raise ExecutionMigrationError("execution_migration_completed_pair_non_text")
    turn_id = _legacy_turn_id(connection, run_id, user)
    committed = CommittedTurn(
        turn_id,
        identity,
        str(user["memory_text"]),
        str(assistant["memory_text"]),
        MemoryScopeRef.personal(identity.actor_id),
        None,
        float(user["created_at"]),
    )
    canonical = committed.canonical_payload()
    result: list[MigrationManifestEntry] = []
    for row in users:
        disposition = (
            LegacyDisposition.KEEP_COMPLETED_PAIR
            if str(row["source_event_id"]) == str(user["source_event_id"])
            else LegacyDisposition.SUPPRESS_TENTATIVE
        )
        result.append(
            _entry(
                row,
                disposition,
                turn_id=turn_id if disposition is LegacyDisposition.KEEP_COMPLETED_PAIR else None,
                terminal_event=terminal_event
                if disposition is LegacyDisposition.KEEP_COMPLETED_PAIR
                else None,
                causal_continuation_id=(
                    continuation_id
                    if disposition is LegacyDisposition.KEEP_COMPLETED_PAIR
                    else None
                ),
                claim_epoch=claim_epoch
                if disposition is LegacyDisposition.KEEP_COMPLETED_PAIR
                else None,
                canonical_turn=canonical
                if disposition is LegacyDisposition.KEEP_COMPLETED_PAIR
                else None,
                canonical_turn_hash=(
                    committed.payload_hash
                    if disposition is LegacyDisposition.KEEP_COMPLETED_PAIR
                    else None
                ),
            )
        )
    result.append(
        _entry(
            assistant,
            LegacyDisposition.KEEP_COMPLETED_PAIR,
            turn_id=turn_id,
            terminal_event=terminal_event,
            causal_continuation_id=continuation_id,
            claim_epoch=claim_epoch,
            canonical_turn=canonical,
            canonical_turn_hash=committed.payload_hash,
        )
    )
    return _ResolvedRun(tuple(result), committed, None, None, None)


def _resolve_terminal_cause(
    connection: sqlite3.Connection, run_id: str, assistant: sqlite3.Row
) -> tuple[str, str | None, int | None]:
    events = connection.execute(
        "SELECT event_id,durable_seq FROM run_events WHERE run_id=? AND kind='run.completed'",
        (run_id,),
    ).fetchall()
    if len(events) != 1:
        raise ExecutionMigrationError("execution_migration_terminal_event_ambiguous")
    event_id = str(events[0]["event_id"])
    maximum = int(
        connection.execute(
            "SELECT COALESCE(MAX(durable_seq),0) FROM run_events WHERE run_id=?", (run_id,)
        ).fetchone()[0]
    )
    if int(events[0]["durable_seq"]) != maximum:
        raise ExecutionMigrationError("execution_migration_terminal_sequence_invalid")
    canonical_root = f"{run_id}:terminal:completed"
    if event_id == canonical_root:
        if assistant["continuation_id"] is not None:
            raise ExecutionMigrationError("execution_migration_terminal_cause_mismatch")
        return event_id, None, None
    prefix = f"{run_id}:terminal:continuation:"
    suffix = ":completed:event"
    if not event_id.startswith(prefix) or not event_id.endswith(suffix):
        raise ExecutionMigrationError("execution_migration_terminal_event_invalid")
    middle = event_id[len(prefix) : -len(suffix)]
    continuation_id, separator, raw_epoch = middle.rpartition(":")
    if not separator or not continuation_id:
        raise ExecutionMigrationError("execution_migration_terminal_event_invalid")
    try:
        claim_epoch = int(raw_epoch)
    except ValueError as error:
        raise ExecutionMigrationError("execution_migration_terminal_event_invalid") from error
    candidates = connection.execute(
        "SELECT c.continuation_id,c.claim_epoch AS continuation_claim_epoch,"
        "c.ack_receipt_id,p.receipt_id,p.claim_epoch AS receipt_claim_epoch "
        "FROM continuations AS c JOIN continuation_progress_receipts AS p "
        "ON p.continuation_id=c.continuation_id WHERE c.run_id=? AND c.continuation_id=?",
        (run_id, continuation_id),
    ).fetchall()
    if len(candidates) != 1:
        raise ExecutionMigrationError("execution_migration_terminal_continuation_ambiguous")
    candidate = candidates[0]
    expected_receipt_id = event_id[: -len("event")] + "receipt"
    if (
        int(candidate["continuation_claim_epoch"]) != claim_epoch
        or str(candidate["ack_receipt_id"]) != str(candidate["receipt_id"])
        or int(candidate["receipt_claim_epoch"]) != claim_epoch
        or str(candidate["receipt_id"]) != expected_receipt_id
    ):
        raise ExecutionMigrationError("execution_migration_terminal_claim_invalid")
    explicit = _optional_str(assistant["continuation_id"])
    if explicit is not None and explicit != continuation_id:
        raise ExecutionMigrationError("execution_migration_terminal_cause_mismatch")
    _validate_terminal_receipt_outcome(
        connection,
        run_id=run_id,
        event_id=event_id,
        receipt_id=expected_receipt_id,
        assistant=assistant,
    )
    return event_id, continuation_id, claim_epoch


def _validate_terminal_receipt_outcome(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    event_id: str,
    receipt_id: str,
    assistant: sqlite3.Row,
) -> None:
    run = connection.execute("SELECT version FROM runs WHERE run_id=?", (run_id,)).fetchone()
    event = connection.execute(
        "SELECT payload_json FROM run_events WHERE event_id=?", (event_id,)
    ).fetchone()
    receipt = connection.execute(
        "SELECT outcome_hash FROM continuation_progress_receipts WHERE receipt_id=?",
        (receipt_id,),
    ).fetchone()
    if run is None or event is None or receipt is None or int(run["version"]) < 1:
        raise ExecutionMigrationError("execution_migration_terminal_receipt_invalid")
    deliveries: list[JsonValue] = []
    for row in connection.execute(
        "SELECT delivery_id,sink_kind,idempotency_key,payload_json FROM delivery_outbox "
        "WHERE run_id=? ORDER BY delivery_id",
        (run_id,),
    ):
        deliveries.append(
            {
                "delivery_id": str(row["delivery_id"]),
                "sink_kind": str(row["sink_kind"]),
                "idempotency_key": str(row["idempotency_key"]),
                "payload": json.loads(str(row["payload_json"])),
            }
        )
    expected = _sha256(
        canonical_json(
            {
                "run_id": run_id,
                "expected_version": int(run["version"]) - 1,
                "terminal_state": "completed",
                "event_id": event_id,
                "payload": json.loads(str(event["payload_json"])),
                "deliveries": deliveries,
                "memory_intent_hash": str(assistant["payload_hash"]),
            }
        )
    )
    if expected != str(receipt["outcome_hash"]):
        raise ExecutionMigrationError("execution_migration_terminal_receipt_invalid")


def _ordered_users(
    connection: sqlite3.Connection, run_id: str, rows: list[sqlite3.Row]
) -> list[sqlite3.Row]:
    sequences = {
        str(row["continuation_id"]): int(row["fifo_seq"])
        for row in connection.execute(
            "SELECT continuation_id,fifo_seq FROM continuations WHERE run_id=?", (run_id,)
        )
    }
    keyed: list[tuple[int, sqlite3.Row]] = []
    for row in rows:
        continuation_id = _optional_str(row["continuation_id"])
        if continuation_id is None:
            key = 0
        elif continuation_id in sequences:
            key = sequences[continuation_id]
        else:
            raise ExecutionMigrationError("execution_migration_legacy_continuation_missing")
        keyed.append((key, row))
    keyed.sort(key=lambda item: item[0])
    if len({item[0] for item in keyed}) != len(keyed):
        raise ExecutionMigrationError("execution_migration_legacy_user_ambiguous")
    return [item[1] for item in keyed]


def _legacy_turn_id(connection: sqlite3.Connection, run_id: str, user: sqlite3.Row) -> str:
    continuation_id = _optional_str(user["continuation_id"])
    if continuation_id is not None:
        return continuation_id
    row = connection.execute(
        "SELECT snapshot_json FROM run_start_snapshots WHERE run_id=?", (run_id,)
    ).fetchone()
    if row is None:
        raise ExecutionMigrationError("execution_migration_start_snapshot_missing")
    try:
        value = json.loads(str(row[0]))
    except json.JSONDecodeError as error:
        raise ExecutionMigrationError("execution_migration_start_snapshot_invalid") from error
    turn_id = value.get("turn_id") if isinstance(value, dict) else None
    return _required(turn_id, "turn_id")


def _entry(
    row: sqlite3.Row,
    disposition: LegacyDisposition,
    *,
    turn_id: str | None = None,
    terminal_event: str | None = None,
    causal_continuation_id: str | None = None,
    claim_epoch: int | None = None,
    canonical_turn: Mapping[str, JsonValue] | None = None,
    canonical_turn_hash: str | None = None,
) -> MigrationManifestEntry:
    source_event_id = str(row["source_event_id"])
    return MigrationManifestEntry(
        source_event_id,
        f"legacy-source:{source_event_id}",
        disposition,
        str(row["payload_hash"]),
        str(row["run_id"]),
        turn_id,
        terminal_event,
        causal_continuation_id,
        claim_epoch,
        canonical_turn,
        canonical_turn_hash,
    )


def _validate_legacy_intent(row: sqlite3.Row) -> None:
    try:
        intent = ConversationMemoryIntent(
            str(row["source_event_id"]),
            str(row["user_id"]),
            str(row["session_id"]),
            ConversationMemoryRole(str(row["role"])),
            None if row["memory_text"] is None else str(row["memory_text"]),
        )
    except (TypeError, ValueError) as error:
        raise ExecutionMigrationError("execution_migration_legacy_intent_invalid") from error
    if intent.payload_hash != str(row["payload_hash"]):
        raise ExecutionMigrationError("execution_migration_legacy_payload_hash")
    run_id = str(row["run_id"])
    continuation_id = _optional_str(row["continuation_id"])
    if str(row["role"]) == "assistant":
        expected_source = f"harness-memory/v1/assistant/{run_id}"
    elif continuation_id is None:
        expected_source = f"harness-memory/v1/user/{run_id}"
    else:
        expected_source = f"harness-memory/v1/user-continuation/{continuation_id}"
    if str(row["source_event_id"]) != expected_source:
        raise ExecutionMigrationError("execution_migration_unknown_source_event")


def _build_v4(
    source: sqlite3.Connection,
    temp: Path,
    identity_map: LegacyIdentityMap,
    manifest: ExecutionMigrationManifest,
    *,
    fault: FaultHook | None,
) -> None:
    from simple_harness.execution.sqlite.database import Database

    target = Database.open(temp)
    try:
        target.connection.execute("PRAGMA foreign_keys = OFF")
        target.connection.execute("DELETE FROM execution_users WHERE user_id='harness-system'")
        special = {
            "sdk_schema_migrations",
            "execution_users",
            "execution_sessions",
            "runs",
            "provider_projection_outbox",
            "memory_outbox",
            "run_start_snapshots",
            "context_preparation_staging",
        }
        source_tables = [
            str(row[0])
            for row in source.execute(
                "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        target_tables = target.table_names()
        for table in source_tables:
            if table in special or table not in target_tables:
                continue
            _copy_table(source, target.connection, table)
            _emit_fault(fault, f"execution_migration.table.{table}.after_copy")
        _copy_execution_identity(source, target.connection, identity_map)
        for table in (
            "execution_users",
            "execution_sessions",
            "runs",
            "provider_projection_outbox",
        ):
            _emit_fault(fault, f"execution_migration.table.{table}.after_copy")
        _copy_snapshots(source, target.connection, identity_map)
        _emit_fault(fault, "execution_migration.table.run_start_snapshots.after_copy")
        _copy_context(source, target.connection, identity_map)
        _emit_fault(fault, "execution_migration.table.context_preparation_staging.after_copy")
        _insert_identity_bindings(target.connection, identity_map)
        _insert_manifest_state(source, target.connection, manifest, identity_map)
        target.connection.execute("PRAGMA foreign_keys = ON")
        if target.integrity_check() != ("ok",) or target.foreign_key_violations():
            raise ExecutionMigrationError("execution_migration_target_integrity")
        _verify_counts(source, target.connection, special, identity_map, manifest)
        target.connection.commit()
    finally:
        target.close()
    os.chmod(temp, 0o600)


def _copy_table(source: sqlite3.Connection, target: sqlite3.Connection, table: str) -> None:
    columns = [str(row[1]) for row in source.execute(f"PRAGMA table_info({_quote(table)})")]
    if not columns:
        return
    names = ",".join(_quote(column) for column in columns)
    placeholders = ",".join("?" for _ in columns)
    rows = source.execute(f"SELECT {names} FROM {_quote(table)}").fetchall()
    target.executemany(
        f"INSERT INTO {_quote(table)}({names}) VALUES({placeholders})",
        (tuple(row[column] for column in columns) for row in rows),
    )


def _copy_execution_identity(
    source: sqlite3.Connection,
    target: sqlite3.Connection,
    identity_map: LegacyIdentityMap,
) -> None:
    identities = identity_map.by_legacy_key()
    session_rows = source.execute(
        "SELECT session_id,user_id,created_at FROM execution_sessions ORDER BY session_id"
    ).fetchall()
    actor_created_at: dict[str, float] = {}
    session_targets: dict[str, AgentIdentity] = {}
    for row in session_rows:
        identity = identities[(str(row["user_id"]), str(row["session_id"]))]
        session_targets[str(row["session_id"])] = identity
        created_at = float(row["created_at"])
        actor_created_at[identity.actor_id] = min(
            actor_created_at.get(identity.actor_id, created_at), created_at
        )
    actor_created_at["harness-system"] = min(actor_created_at.get("harness-system", 0.0), 0.0)
    target.executemany(
        "INSERT INTO execution_users VALUES(?,?)",
        sorted(actor_created_at.items()),
    )
    target.executemany(
        "INSERT INTO execution_sessions VALUES(?,?,?)",
        (
            (
                session_targets[str(row["session_id"])].session_id,
                session_targets[str(row["session_id"])].actor_id,
                float(row["created_at"]),
            )
            for row in session_rows
        ),
    )
    _copy_rows_with_session_remap(source, target, "runs", session_targets)
    _copy_rows_with_session_remap(source, target, "provider_projection_outbox", session_targets)


def _copy_rows_with_session_remap(
    source: sqlite3.Connection,
    target: sqlite3.Connection,
    table: str,
    session_targets: Mapping[str, AgentIdentity],
) -> None:
    columns = [str(row[1]) for row in source.execute(f"PRAGMA table_info({_quote(table)})")]
    session_index = columns.index("execution_session_id")
    names = ",".join(_quote(column) for column in columns)
    placeholders = ",".join("?" for _ in columns)
    for row in source.execute(f"SELECT {names} FROM {_quote(table)}"):
        values = [row[column] for column in columns]
        values[session_index] = session_targets[str(row["execution_session_id"])].session_id
        target.execute(
            f"INSERT INTO {_quote(table)}({names}) VALUES({placeholders})",
            values,
        )


def _copy_snapshots(
    source: sqlite3.Connection,
    target: sqlite3.Connection,
    identity_map: LegacyIdentityMap,
) -> None:
    identities = identity_map.by_legacy_key()
    sessions = {
        str(row["run_id"]): (str(row["user_id"]), str(row["execution_session_id"]))
        for row in source.execute(
            "SELECT r.run_id,r.execution_session_id,s.user_id FROM runs AS r "
            "JOIN execution_sessions AS s ON s.session_id=r.execution_session_id"
        )
    }
    for row in source.execute("SELECT * FROM run_start_snapshots ORDER BY run_id"):
        raw = str(row["snapshot_json"])
        if _sha256(raw) != str(row["snapshot_hash"]):
            raise ExecutionMigrationError("execution_migration_start_snapshot_hash")
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as error:
            raise ExecutionMigrationError("execution_migration_start_snapshot_invalid") from error
        if not isinstance(value, dict):
            raise ExecutionMigrationError("execution_migration_start_snapshot_invalid")
        conversation = value.get("conversation")
        if isinstance(conversation, dict) and "identity" not in conversation:
            identity = identities[sessions[str(row["run_id"])]]
            conversation = dict(conversation)
            conversation.pop("user_id", None)
            conversation.pop("session_id", None)
            conversation["identity"] = identity.to_json()
            conversation["recall_scopes"] = [
                MemoryScopeRef.personal(identity.actor_id).to_json(),
                MemoryScopeRef.family(identity.household_id).to_json(),
            ]
            conversation["context_source_snapshot_ref"] = None
            value["conversation"] = conversation
        migrated = canonical_json(cast(dict[str, JsonValue], value))
        target.execute(
            "INSERT INTO run_start_snapshots VALUES(?,?,?,?)",
            (str(row["run_id"]), migrated, _sha256(migrated), float(row["created_at"])),
        )


def _copy_context(
    source: sqlite3.Connection,
    target: sqlite3.Connection,
    identity_map: LegacyIdentityMap,
) -> None:
    identities = identity_map.by_legacy_key()
    old_columns = [
        str(row[1]) for row in source.execute("PRAGMA table_info(context_preparation_staging)")
    ]
    names = ",".join(_quote(column) for column in old_columns)
    placeholders = ",".join("?" for _ in old_columns)
    rows = source.execute(f"SELECT {names} FROM context_preparation_staging").fetchall()
    for row in rows:
        values = [row[column] for column in old_columns]
        identity = identities[(str(row["user_id"]), str(row["session_id"]))]
        values[old_columns.index("user_id")] = identity.actor_id
        values[old_columns.index("session_id")] = identity.session_id
        target.execute(
            f"INSERT INTO context_preparation_staging({names}) VALUES({placeholders})",
            values,
        )


def _insert_identity_bindings(
    connection: sqlite3.Connection, identity_map: LegacyIdentityMap
) -> None:
    for item in identity_map.bindings:
        identity_json = canonical_json(item.identity.to_json())
        connection.execute(
            "INSERT INTO agent_identity_bindings VALUES(?,?,?,?,?,?)",
            (
                item.identity.session_id,
                item.identity.deployment_id,
                item.identity.household_id,
                item.identity.actor_id,
                _sha256(identity_json),
                0.0,
            ),
        )


def _insert_manifest_state(
    source: sqlite3.Connection,
    target: sqlite3.Connection,
    manifest: ExecutionMigrationManifest,
    identity_map: LegacyIdentityMap,
) -> None:
    legacy_rows = {
        str(row["source_event_id"]): row for row in source.execute("SELECT * FROM memory_outbox")
    }
    for entry in manifest.entries:
        target.execute(
            "INSERT INTO legacy_memory_dispositions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                entry.source_key,
                "legacy-source",
                entry.source_event_id,
                entry.run_id,
                entry.turn_id,
                entry.disposition.value,
                entry.payload_hash,
                (
                    None
                    if entry.canonical_turn is None
                    else canonical_json(dict(entry.canonical_turn))
                ),
                entry.canonical_turn_hash,
                entry.causal_terminal_event_id,
                entry.causal_continuation_id,
                entry.causal_claim_epoch,
                float(legacy_rows[entry.source_event_id]["created_at"]),
                float(legacy_rows[entry.source_event_id]["created_at"]),
            ),
        )
    by_run: dict[str, list[MigrationManifestEntry]] = {}
    for entry in manifest.entries:
        by_run.setdefault(entry.run_id, []).append(entry)
    identities = identity_map.by_legacy_key()
    for run_id, entries in by_run.items():
        keep = [
            entry for entry in entries if entry.disposition is LegacyDisposition.KEEP_COMPLETED_PAIR
        ]
        deferred = [
            entry for entry in entries if entry.disposition is LegacyDisposition.DEFERRED_TURN
        ]
        if keep:
            canonical_turn = keep[0].canonical_turn
            if canonical_turn is None:
                raise ExecutionMigrationError("execution_migration_manifest_turn_missing")
            turn = CommittedTurn.from_json(canonical_turn)
            target.execute(
                "INSERT INTO memory_outbox VALUES(?,?,?,?,?,?,?,?,?,'pending',"
                "NULL,0,NULL,0,?,NULL,?,NULL)",
                (
                    f"agent-memory-turn/v1/{turn.turn_id}",
                    run_id,
                    turn.turn_id,
                    turn.identity.deployment_id,
                    turn.identity.household_id,
                    turn.identity.actor_id,
                    turn.identity.session_id,
                    canonical_json(turn.canonical_payload()),
                    turn.payload_hash,
                    turn.turn_started_at,
                    turn.turn_started_at,
                ),
            )
        if deferred:
            if len(deferred) != 1:
                raise ExecutionMigrationError("execution_migration_deferred_ambiguous")
            entry = deferred[0]
            row = legacy_rows[entry.source_event_id]
            run = source.execute(
                "SELECT r.execution_session_id,s.user_id FROM runs AS r "
                "JOIN execution_sessions AS s ON s.session_id=r.execution_session_id "
                "WHERE r.run_id=?",
                (run_id,),
            ).fetchone()
            identity = identities[(str(run["user_id"]), str(run["execution_session_id"]))]
            user_text = str(row["memory_text"])
            input_hash = _cursor_hash(entry.turn_id or "", user_text)
            target.execute(
                "INSERT INTO legacy_turn_cursors VALUES(?,1,?,'legacy-source',"
                "?,?,?,?,NULL,?,'active',"
                "NULL,NULL,?,?,NULL)",
                (
                    run_id,
                    entry.source_key,
                    entry.source_event_id,
                    entry.turn_id,
                    user_text,
                    input_hash,
                    float(row["created_at"]),
                    float(row["created_at"]),
                    float(row["created_at"]),
                ),
            )
            binding = target.execute(
                "SELECT actor_id FROM agent_identity_bindings WHERE session_id=?",
                (identity.session_id,),
            ).fetchone()
            if binding is None or str(binding[0]) != identity.actor_id:
                raise ExecutionMigrationError("execution_migration_cursor_identity")


def _verify_counts(
    source: sqlite3.Connection,
    target: sqlite3.Connection,
    special: set[str],
    identity_map: LegacyIdentityMap,
    manifest: ExecutionMigrationManifest,
) -> None:
    for row in source.execute(
        "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ):
        table = str(row[0])
        if table in special:
            continue
        source_count = int(source.execute(f"SELECT COUNT(*) FROM {_quote(table)}").fetchone()[0])
        target_count = int(target.execute(f"SELECT COUNT(*) FROM {_quote(table)}").fetchone()[0])
        if source_count != target_count:
            raise ExecutionMigrationError("execution_migration_table_count")
    for table in ("run_start_snapshots", "context_preparation_staging"):
        source_count = int(source.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        target_count = int(target.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        if source_count != target_count:
            raise ExecutionMigrationError("execution_migration_table_count")
    for table in ("execution_sessions", "runs", "provider_projection_outbox"):
        source_count = int(source.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        target_count = int(target.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        if source_count != target_count:
            raise ExecutionMigrationError("execution_migration_table_count")
    expected_users = {"harness-system"} | {
        binding.identity.actor_id for binding in identity_map.bindings
    }
    actual_users = {str(row[0]) for row in target.execute("SELECT user_id FROM execution_users")}
    if actual_users != expected_users:
        raise ExecutionMigrationError("execution_migration_user_mapping_count")
    expected_sessions = {binding.identity.session_id for binding in identity_map.bindings}
    actual_sessions = {
        str(row[0]) for row in target.execute("SELECT session_id FROM execution_sessions")
    }
    bound_sessions = {
        str(row[0]) for row in target.execute("SELECT session_id FROM agent_identity_bindings")
    }
    if actual_sessions != expected_sessions or bound_sessions != expected_sessions:
        raise ExecutionMigrationError("execution_migration_identity_mapping_count")
    expected_turn_runs = {
        entry.run_id
        for entry in manifest.entries
        if entry.disposition is LegacyDisposition.KEEP_COMPLETED_PAIR
    }
    actual_turn_runs = {str(row[0]) for row in target.execute("SELECT run_id FROM memory_outbox")}
    expected_cursor_runs = {
        entry.run_id
        for entry in manifest.entries
        if entry.disposition is LegacyDisposition.DEFERRED_TURN
    }
    actual_cursor_runs = {
        str(row[0]) for row in target.execute("SELECT run_id FROM legacy_turn_cursors")
    }
    disposition_count = int(
        target.execute("SELECT COUNT(*) FROM legacy_memory_dispositions").fetchone()[0]
    )
    if (
        actual_turn_runs != expected_turn_runs
        or actual_cursor_runs != expected_cursor_runs
        or disposition_count != len(manifest.entries)
    ):
        raise ExecutionMigrationError("execution_migration_manifest_state_count")


def _validate_replaced_v4(path: Path, manifest: ExecutionMigrationManifest) -> None:
    from simple_harness.execution.sqlite.database import Database

    with Database.open(path) as database:
        dispositions = int(
            database.connection.execute(
                "SELECT COUNT(*) FROM legacy_memory_dispositions"
            ).fetchone()[0]
        )
        if dispositions != len(manifest.entries):
            raise ExecutionMigrationError("execution_migration_manifest_readback")


def _cursor_hash(turn_id: str, user_text: str) -> str:
    return _sha256(
        canonical_json(
            {
                "protocol": "simple-harness/legacy-turn-cursor/v1",
                "turn_id": turn_id,
                "user_text": user_text,
            }
        )
    )


def _quote(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _required(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError(f"{name} is required")
    return value


def _optional_required(value: object, name: str) -> str | None:
    return None if value is None else _required(value, name)


def _digest(value: object, name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _optional_digest(value: object, name: str) -> str | None:
    return None if value is None else _digest(value, name)


def _optional_str(value: object) -> str | None:
    return None if value is None else str(value)


def _emit_fault(fault: FaultHook | None, point: str) -> None:
    if fault is not None:
        fault(point)


__all__ = (
    "ExecutionMigrationError",
    "ExecutionMigrationManifest",
    "LegacyDisposition",
    "LegacyIdentityBinding",
    "LegacyIdentityMap",
    "MigrationManifestEntry",
    "migrate_execution_v3_to_v4",
)
