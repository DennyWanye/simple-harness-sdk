"""Trusted consumer mode, pinned in the same transaction as accepting a Run.

The source reference is an existing canonical SDK admission/command/start fact.
This is not a second Memory authority and never accepts model metadata.
"""

from simple_harness.execution.context_use import _text, use_hash

DDL = """
CREATE TABLE run_context_use_requirements (
 run_id TEXT PRIMARY KEY, authority_scope_ref TEXT,
 source_kind TEXT NOT NULL CHECK(source_kind IN ('legacy_admission','start_command','run_start')),
 source_ref TEXT NOT NULL, source_hash TEXT NOT NULL CHECK(length(source_hash)=64),
 requirement_hash TEXT NOT NULL CHECK(length(requirement_hash)=64)
) STRICT;
CREATE TRIGGER run_context_use_requirement_no_update BEFORE UPDATE ON run_context_use_requirements
 BEGIN SELECT RAISE(ABORT,'context_use_requirement_immutable'); END;
CREATE TRIGGER run_context_use_requirement_no_delete BEFORE DELETE ON run_context_use_requirements
 BEGIN SELECT RAISE(ABORT,'context_use_requirement_immutable'); END;
"""


def read(connection, run_id):
    row = connection.execute(
        "SELECT * FROM run_context_use_requirements WHERE run_id=?", (run_id,)
    ).fetchone()
    if row is None:
        return None
    value = dict(row)
    expected = value.pop("requirement_hash")
    if use_hash("simple-harness/run-context-use-requirement/v1", value) != expected:
        raise ValueError("context_use_requirement_corrupt")
    kind = value["source_kind"]
    if kind == "legacy_admission":
        source = connection.execute(
            "SELECT run_id,intent_hash FROM conversation_run_modes WHERE run_id=?",
            (value["source_ref"],),
        ).fetchone()
    elif kind == "start_command":
        source = connection.execute(
            "SELECT run_id,intent_hash FROM conversation_commands WHERE command_id=? AND kind='start'",
            (value["source_ref"],),
        ).fetchone()
    else:
        source = connection.execute(
            "SELECT run_id,snapshot_hash FROM run_start_snapshots WHERE run_id=?",
            (value["source_ref"],),
        ).fetchone()
    if source is None or tuple(source) != (run_id, value["source_hash"]):
        raise ValueError("context_use_requirement_source_differs")
    return value


def require(connection, run_id, scope):
    row = read(connection, run_id)
    if row is None:
        if scope is not None:
            raise ValueError("context_use_legacy_admission_unverified")
        return  # Explicit legacy no-Memory compatibility, not an empty attestation.
    if row["authority_scope_ref"] != scope:
        raise ValueError("context_use_admission_scope_differs")


def bind(connection, run_id, scope, source_kind, source_ref, source_hash):
    _text(run_id)
    if scope is not None:
        _text(scope)
    prior = read(connection, run_id)
    if prior is not None:
        require(connection, run_id, scope)
        return
    value = dict(
        run_id=run_id,
        authority_scope_ref=scope,
        source_kind=source_kind,
        source_ref=source_ref,
        source_hash=source_hash,
    )
    connection.execute(
        "INSERT INTO run_context_use_requirements VALUES (?,?,?,?,?,?)",
        (*value.values(), use_hash("simple-harness/run-context-use-requirement/v1", value)),
    )


def validate_recovery(connection, scope):
    # Check before reconciliation/claiming/driver activation, without mutating a Run.
    active = connection.execute("""SELECT run_id FROM runs WHERE state NOT IN ('completed','failed','cancelled')
        UNION SELECT run_id FROM conversation_commands WHERE state NOT IN ('applied','rejected','cancelled')""")
    for row in active:
        require(connection, row[0], scope)
