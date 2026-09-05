"""Explicit observational schema, independent of the execution7 authority schema."""

import sqlite3
import time
from functools import lru_cache

from simple_harness.execution.audit import RunAuditUnavailable, audit_hash

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


class AuditSchemaIncompatible(RunAuditUnavailable):
    code = "audit_schema_incompatible"


def validate_audit_schema(connection):
    found = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}
    if not OBJECTS <= found:
        raise AuditSchemaIncompatible("audit_schema_partial_or_unavailable")
    rows = [
        tuple(row) for row in connection.execute("SELECT version,checksum FROM sdk_audit_schema")
    ]
    if rows != [(AUDIT_SCHEMA_VERSION, CHECKSUM)]:
        raise AuditSchemaIncompatible("audit_schema_version_or_checksum_unavailable")
    if _objects(connection) != _expected_objects():
        raise AuditSchemaIncompatible("audit_schema_structure_unavailable")


def _objects(connection):
    return tuple(
        (name, kind, table, " ".join(sql.split()) if sql is not None else None)
        for name, kind, table, sql in connection.execute(
            "SELECT name,type,tbl_name,sql FROM sqlite_master WHERE name IN ("
            + ",".join("?" for _ in OBJECTS)
            + ") ORDER BY name",
            tuple(sorted(OBJECTS)),
        )
    )


@lru_cache(maxsize=1)
def _expected_objects():
    connection = sqlite3.connect(":memory:")
    try:
        for statement in DDL:
            connection.execute(statement)
        return _objects(connection)
    finally:
        connection.close()


def ensure_audit_schema(database):
    with database.transaction() as connection:
        found = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}
        if OBJECTS & found:
            validate_audit_schema(connection)
        else:
            for statement in DDL:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO sdk_audit_schema VALUES (?,?)", (AUDIT_SCHEMA_VERSION, CHECKSUM)
            )
        from .command_audit import record_command_event

        for row in connection.execute(
            "SELECT command_id FROM conversation_commands c WHERE NOT EXISTS "
            "(SELECT 1 FROM sdk_command_audit_events a WHERE a.command_id=c.command_id)",
        ):
            # A present head is an observed legacy baseline, never reconstructed
            # admission or retries. It also supplies an immutable snapshot cut.
            record_command_event(connection, row[0], "legacy_baseline", now=time.time())
