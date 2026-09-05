"""Same-transaction witnesses for SDK canonical events, including workflow adapters."""

from simple_harness.contracts import canonical_json
from simple_harness.execution.audit import audit_hash
from simple_harness.execution.runtime_audit import current_operation_binding


def record_event_witness(connection, event_id):
    source = dict(
        connection.execute(
            "SELECT * FROM run_events WHERE event_id=?",
            (event_id,),
        ).fetchone()
    )
    if source["kind"].startswith("audit."):
        return
    proof = dict(
        source_id=event_id,
        source_hash=audit_hash(source),
        source_sequence=source["durable_seq"],
        contract="sdk.core.v2",
        birth=source["kind"] in {"run.created", "child.created"},
        runtime_operation=current_operation_binding(source["run_id"]),
    )
    connection.execute(
        "INSERT INTO run_events(event_id,run_id,durable_seq,kind,payload_json,created_at) "
        "SELECT ?,?,COALESCE(MAX(durable_seq),0)+1,'audit.event.v2',?,? "
        "FROM run_events WHERE run_id=?",
        (
            "audit-event:" + audit_hash(proof),
            source["run_id"],
            canonical_json(proof),
            source["created_at"],
            source["run_id"],
        ),
    )


CLAIM_SOURCES = {
    "continuations": ("continuation_id", "run_id", "control"),
    "child_signals": ("signal_id", "parent_run_id", "child"),
}


def record_claim_witness(connection, table, identity, *, now):
    key, owner, _ = CLAIM_SOURCES[table]
    row = dict(connection.execute(f"SELECT * FROM {table} WHERE {key}=?", (identity,)).fetchone())
    payload = dict(
        table=table,
        identity_hash=audit_hash(identity),
        source_hash=audit_hash(row),
        source_version=row["version"],
        claim_epoch=row["claim_epoch"],
        runtime_epoch=row.get("runtime_lease_epoch"),
        owner_hash=audit_hash(row["claimed_by"]),
        contract="sdk.core.v2",
    )
    connection.execute(
        "INSERT INTO run_events(event_id,run_id,durable_seq,kind,payload_json,created_at) "
        "SELECT ?,?,COALESCE(MAX(durable_seq),0)+1,'audit.claim.v2',?,? "
        "FROM run_events WHERE run_id=?",
        (
            "audit-claim:" + audit_hash(payload),
            row[owner],
            canonical_json(payload),
            now,
            row[owner],
        ),
    )
