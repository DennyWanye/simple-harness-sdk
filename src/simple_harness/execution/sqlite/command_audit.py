"""Command mutation witnesses, including intents whose Run does not yet exist."""

from simple_harness.execution.audit import (
    RunAuditUnavailable,
    RunOperationAuditV1,
    audit_error_code,
    audit_hash,
    audit_reference,
)

OPERATIONS = frozenset(
    {
        "accepted",
        "claimed",
        "transition",
        "retry",
        "rejected",
        "heartbeat",
        "applied",
        "cancelled",
        "legacy_baseline",
    }
)
IDENTITY = (
    "command_id",
    "namespace",
    "projection_key_id",
    "run_id",
    "kind",
    "accept_seq",
    "intent_hash",
    "created_at",
)


def command_identity(row):
    return audit_hash({key: row[key] for key in IDENTITY})


def record_command_event(
    connection, command_id, operation, *, now, claim=None, cause_command_id=None
):
    if operation not in OPERATIONS:
        raise ValueError("unknown command audit operation")
    row = connection.execute(
        "SELECT * FROM conversation_commands WHERE command_id=?", (command_id,)
    ).fetchone()
    if row is None:
        raise RunAuditUnavailable("command_audit_source_missing")
    owner = row["owner_id"] if claim is None else claim.owner_id
    if claim is not None and claim.claim_epoch != row["claim_epoch"]:
        raise RunAuditUnavailable("command_audit_claim_mismatch")
    error = (
        row["last_error_code"] if operation in {"retry", "rejected", "legacy_baseline"} else None
    )
    connection.execute(
        "INSERT INTO sdk_command_audit_events "
        "(command_id,run_id,identity_hash,command_version,operation,state,claim_epoch,"
        "attempt_count,source_hash,owner_ref_hash,cause_command_id,"
        "error_code,error_code_hash,created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            command_id,
            row["run_id"],
            command_identity(row),
            row["version"],
            operation,
            row["state"],
            row["claim_epoch"],
            row["attempt_count"],
            audit_hash(dict(row)),
            None if owner is None else audit_hash(owner),
            cause_command_id,
            audit_error_code(error),
            None if error is None else audit_hash(error),
            now,
        ),
    )


def command_event_operation(row):
    if row["operation"] not in OPERATIONS:
        raise RunAuditUnavailable("command_audit_operation_invalid")
    return RunOperationAuditV1(
        "control:" + row["command_id"],
        "control",
        row["state"],
        row["command_version"],
        row["source_hash"],
        "command-event:" + str(row["event_seq"]),
        record_type="receipt" if row["operation"] == "legacy_baseline" else "transition",
        operation_name="command." + row["operation"],
        error_code_hash=row["error_code_hash"],
        created_at=row["created_at"],
        error_code=audit_error_code(row["error_code"]),
        claim_epoch=row["claim_epoch"],
        attempt_count=row["attempt_count"],
        owner_ref_hash=row["owner_ref_hash"],
        causal_command_ref=audit_reference("control", row["cause_command_id"]),
    )


def command_coverage(connection, row):
    events = connection.execute(
        "SELECT * FROM sdk_command_audit_events WHERE command_id=? ORDER BY command_version",
        (row["command_id"],),
    )
    expected, last, gaps = 1, None, set()
    for event in events:
        if event["identity_hash"] != command_identity(row):
            raise RunAuditUnavailable("command_audit_identity_mismatch")
        if event["command_version"] != expected:
            gaps.add("command_transition_history_unverified")
        if expected == 1 and (event["command_version"] != 1 or event["operation"] != "accepted"):
            gaps.add("command_introduction_unverified")
        expected = event["command_version"] + 1
        last = event
    if last is None:
        gaps.add("command_introduction_unverified")
    if (
        last is None
        or last["command_version"] != row["version"]
        or last["source_hash"] != audit_hash(dict(row))
    ):
        gaps.add("command_transition_history_unverified")
    return tuple(sorted(gaps))


class _CommandHeader(dict):
    def to_json(self):
        return dict(self)


def read_command_snapshot(connection, command_id, operation_sink):
    row = connection.execute(
        "SELECT * FROM conversation_commands WHERE command_id=?", (command_id,)
    ).fetchone()
    if row is None:
        raise RunAuditUnavailable("command_not_found")
    count = 0
    for event in connection.execute(
        "SELECT * FROM sdk_command_audit_events WHERE command_id=? ORDER BY command_version",
        (command_id,),
    ):
        operation_sink.append(command_event_operation(event))
        count += 1
    if count == 0:
        raise RunAuditUnavailable("command_audit_introduction_unavailable")
    gaps = command_coverage(connection, row)
    return _CommandHeader(
        schema_version=1,
        command_ref=audit_reference("control", command_id),
        target_run_ref=audit_reference("run", row["run_id"]),
        namespace_ref=audit_reference("command_namespace", row["namespace"]),
        command_version=row["version"],
        run_materialized=connection.execute(
            "SELECT 1 FROM runs WHERE run_id=?", (row["run_id"],)
        ).fetchone()
        is not None,
        coverage_gaps=list(gaps),
        history_coverage="partial" if gaps else "recorded",
        recording_contract_version=1,
        recording_coverage="unverified" if gaps else "verified_current_intervals",
        source_set=["conversation_commands", "sdk_command_audit_events"],
        operations=[],
        truncated=False,
        current_source_complete=True,
        snapshot_hash=None,
    )


def command_incarnation(connection, command_id):
    row = connection.execute(
        "SELECT * FROM conversation_commands WHERE command_id=?", (command_id,)
    ).fetchone()
    if row is None:
        raise RunAuditUnavailable("command_not_found")
    namespace = connection.execute(
        "SELECT * FROM conversation_command_namespaces WHERE namespace=?",
        (row["namespace"],),
    ).fetchone()
    if namespace is None or namespace["projection_key_id"] != row["projection_key_id"]:
        raise RunAuditUnavailable("command_namespace_unavailable")
    return audit_hash([command_identity(row), dict(namespace)])


def command_cut(connection, command_id=None, *, run_id=None, sequence=None):
    owner, identity = ("command_id", command_id) if command_id is not None else ("run_id", run_id)
    if sequence == 0:
        return dict(sequence=0, source_hash=None)
    if sequence is None:
        row = connection.execute(
            f"SELECT * FROM sdk_command_audit_events WHERE {owner}=? "
            "ORDER BY event_seq DESC LIMIT 1",
            (identity,),
        ).fetchone()
    else:
        row = connection.execute(
            f"SELECT * FROM sdk_command_audit_events WHERE {owner}=? AND event_seq=?",
            (identity, sequence),
        ).fetchone()
    if row is None:
        if command_id is not None or sequence is not None:
            raise RunAuditUnavailable("command_audit_cut_unavailable")
        return dict(sequence=0, source_hash=None)
    return dict(sequence=row["event_seq"], source_hash=audit_hash(dict(row)))
