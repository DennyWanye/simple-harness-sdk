"""Explicit observational schema, independent of the execution7 authority schema."""

import sqlite3
import time
from functools import lru_cache

from simple_harness.execution.audit import RunAuditUnavailable, audit_hash

from .stage_audit_schema import STAGE_DDL, STAGE_OBJECTS

AUDIT_SCHEMA_VERSION = 1
DDL = (
    "CREATE TABLE sdk_audit_schema (version INTEGER PRIMARY KEY, checksum TEXT NOT NULL) STRICT",
    """CREATE TABLE sdk_command_audit_events (
        event_seq INTEGER PRIMARY KEY AUTOINCREMENT,
        command_id TEXT NOT NULL REFERENCES conversation_commands(command_id),
        run_id TEXT NOT NULL,
        identity_hash TEXT NOT NULL,
        command_version INTEGER NOT NULL,
        operation TEXT NOT NULL,
        state TEXT NOT NULL,
        claim_epoch INTEGER NOT NULL,
        attempt_count INTEGER NOT NULL,
        source_hash TEXT NOT NULL,
        owner_ref_hash TEXT,
        cause_command_id TEXT REFERENCES conversation_commands(command_id),
        error_code TEXT,
        error_code_hash TEXT,
        created_at REAL NOT NULL,
        UNIQUE(command_id, command_version)
    ) STRICT""",
    "CREATE INDEX sdk_command_audit_run_idx ON sdk_command_audit_events(run_id,event_seq)",
    """CREATE TRIGGER sdk_command_audit_no_update BEFORE UPDATE ON sdk_command_audit_events
        BEGIN SELECT RAISE(ABORT, 'command audit is append only'); END""",
    """CREATE TRIGGER sdk_command_audit_no_delete BEFORE DELETE ON sdk_command_audit_events
        BEGIN SELECT RAISE(ABORT, 'command audit is append only'); END""",
)
CHECKSUM = audit_hash(list(DDL))
OBJECTS = {
    "sdk_audit_schema",
    "sdk_command_audit_events",
    "sdk_command_audit_run_idx",
    "sdk_command_audit_no_update",
    "sdk_command_audit_no_delete",
}


V1_DDL, V1_CHECKSUM, V1_OBJECTS = DDL, CHECKSUM, frozenset(OBJECTS)

AUDIT_SCHEMA_VERSION = 2
DDL = V1_DDL + STAGE_DDL
CHECKSUM = audit_hash(list(DDL))
OBJECTS = V1_OBJECTS | STAGE_OBJECTS


class AuditSchemaIncompatible(RunAuditUnavailable):
    code = "audit_schema_incompatible"


def validate_audit_schema(connection, *, allow_v1=False):
    found = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}
    if not V1_OBJECTS <= found:
        raise AuditSchemaIncompatible("audit_schema_partial_or_unavailable")
    # Descriptor shape must be checked before any SELECT of its columns.
    descriptor = {"sdk_audit_schema"}
    if _objects(connection, descriptor) != tuple(
        row for row in _expected_objects(1) if row[0] in descriptor
    ):
        raise AuditSchemaIncompatible("audit_schema_structure_unavailable")
    rows = [
        tuple(row) for row in connection.execute("SELECT version,checksum FROM sdk_audit_schema")
    ]
    if rows == [(1, V1_CHECKSUM)] and allow_v1:
        if STAGE_OBJECTS & found or _objects(connection, V1_OBJECTS) != _expected_objects(1):
            raise AuditSchemaIncompatible("audit_schema_structure_unavailable")
        return 1
    if rows != [(AUDIT_SCHEMA_VERSION, CHECKSUM)]:
        raise AuditSchemaIncompatible("audit_schema_version_or_checksum_unavailable")
    if not OBJECTS <= found or _objects(connection) != _expected_objects(2):
        raise AuditSchemaIncompatible("audit_schema_structure_unavailable")
    return AUDIT_SCHEMA_VERSION


def _objects(connection, objects=None):
    objects = OBJECTS if objects is None else objects
    return tuple(
        (name, kind, table, " ".join(sql.split()) if sql is not None else None)
        for name, kind, table, sql in connection.execute(
            "SELECT name,type,tbl_name,sql FROM sqlite_master WHERE name IN ("
            + ",".join("?" for _ in objects)
            + ") ORDER BY name",
            tuple(sorted(objects)),
        )
    )


@lru_cache(maxsize=2)
def _expected_objects(version):
    connection = sqlite3.connect(":memory:")
    try:
        if version == 2:
            from .schema import fresh_descriptor

            connection.executescript(fresh_descriptor().sql)
        for statement in V1_DDL if version == 1 else DDL:
            connection.execute(statement)
        return _objects(connection, V1_OBJECTS if version == 1 else OBJECTS)
    finally:
        connection.close()


def ensure_audit_schema(database):
    with database.transaction() as connection:
        ensure_audit_schema_on(connection)


def ensure_audit_schema_on(connection):
    """Bootstrap or upgrade the explicit audit schema on an open write transaction.

    Also used by the explicit v7/v8 -> v9 upgrader for libraries written before the
    audit schema existed (they can no longer be opened directly).
    """
    found = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}
    if OBJECTS & found:
        version = validate_audit_schema(connection, allow_v1=True)
        if version == 1:
            for statement in STAGE_DDL:
                connection.execute(statement)
            connection.execute(
                "UPDATE sdk_audit_schema SET version=?,checksum=?",
                (AUDIT_SCHEMA_VERSION, CHECKSUM),
            )
    else:
        for statement in DDL:
            connection.execute(statement)
        connection.execute(
            "INSERT INTO sdk_audit_schema VALUES (?,?)", (AUDIT_SCHEMA_VERSION, CHECKSUM)
        )
    from .stage_audit_schema import seed_observed_legacy

    seed_observed_legacy(connection)
    from .command_audit import record_command_event

    for row in connection.execute(
        "SELECT command_id FROM conversation_commands c WHERE NOT EXISTS "
        "(SELECT 1 FROM sdk_command_audit_events a WHERE a.command_id=c.command_id)",
    ):
        # A present head is an observed legacy baseline, never reconstructed
        # admission or retries. It also supplies an immutable snapshot cut.
        record_command_event(connection, row[0], "legacy_baseline", now=time.time())
