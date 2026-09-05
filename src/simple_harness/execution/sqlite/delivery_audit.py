"""Actual SDK delivery versions and conservative physical handoff facts in run_events."""

import json

from simple_harness.contracts import canonical_json
from simple_harness.execution.audit import RunAuditUnavailable, RunOperationAuditV1, audit_hash

KIND = "audit.delivery.v1"


def _append(connection, row, value, now):
    connection.execute(
        "INSERT INTO run_events(event_id,run_id,durable_seq,kind,payload_json,created_at) "
        "SELECT ?,?,COALESCE(MAX(durable_seq),0)+1,?,?,? FROM run_events WHERE run_id=?",
        (
            "audit-delivery:" + audit_hash(value),
            row["run_id"],
            KIND,
            canonical_json(value),
            now,
            row["run_id"],
        ),
    )


def _row(connection, identity):
    return dict(
        connection.execute(
            "SELECT * FROM delivery_outbox WHERE delivery_id=?", (identity,)
        ).fetchone()
    )


def _identity(row):
    return audit_hash(
        {
            key: row[key]
            for key in (
                "delivery_id",
                "run_id",
                "sink_kind",
                "idempotency_key",
                "payload_json",
                "created_at",
            )
        }
    )


def record_version(connection, identity, *, now, operation):
    row = _row(connection, identity)
    _append(
        connection,
        row,
        dict(
            domain="version",
            identity_hash=_identity(row),
            delivery_hash=audit_hash(identity),
            version=row["version"],
            state=row["state"],
            operation=operation,
            source_hash=audit_hash(row),
            sink_hash=audit_hash(row["sink_kind"]),
            request_hash=audit_hash(json.loads(row["payload_json"])),
        ),
        now,
    )


def handoff(connection, identity, *, expected_version, now):
    from simple_harness.execution.delivery import DeliveryConflictError

    row = _row(connection, identity)
    if row["state"] != "claimed" or row["version"] != expected_version:
        raise DeliveryConflictError("delivery handoff claim is stale")
    connection.execute(
        "UPDATE delivery_outbox SET version=version+1 WHERE delivery_id=? AND version=?",
        (identity, expected_version),
    )
    row = _row(connection, identity)
    record_version(connection, identity, now=now, operation="handoff")
    value = dict(
        domain="attempt",
        identity_hash=_identity(row),
        delivery_hash=audit_hash(identity),
        version=row["version"],
        state="handoff",
        source_hash=audit_hash(row),
        sink_hash=audit_hash(row["sink_kind"]),
        request_hash=audit_hash(json.loads(row["payload_json"])),
    )
    # One actual dispatcher handoff per claim; duplicate call cannot invoke a sink again.
    _append(connection, row, value, now)


def settle_attempt(connection, identity, *, expected_version, now, state, error_code=None):
    row = _row(connection, identity)
    starts = [
        value
        for _, value in facts(connection, row["run_id"])
        if value["domain"] == "attempt"
        and value["state"] == "handoff"
        and value["delivery_hash"] == audit_hash(identity)
        and value["version"] == expected_version
    ]
    if not starts:
        return  # Direct claim/complete is a logical receipt, not a proven sink call.
    value = dict(
        starts[0],
        state=state,
        error_code=("delivery_sink_exception" if error_code == "delivery_sink_exception" else None),
    )
    _append(connection, row, value, now)


def facts(connection, run_id):
    for row in connection.execute(
        "SELECT * FROM run_events WHERE run_id=? AND kind=? ORDER BY durable_seq",
        (run_id, KIND),
    ):
        value = json.loads(row["payload_json"])
        if row["event_id"] != "audit-delivery:" + audit_hash(value):
            raise RunAuditUnavailable("delivery_audit_hash_mismatch")
        yield row, value


def operations(connection, run_id):
    for stored in connection.execute(
        "SELECT * FROM delivery_outbox WHERE run_id=? ORDER BY delivery_id", (run_id,)
    ):
        stored = dict(stored)
        yield RunOperationAuditV1(
            "delivery:" + _identity(stored),
            "delivery",
            stored["state"],
            stored["version"],
            audit_hash(stored),
            "delivery:" + audit_hash(stored["delivery_id"]),
            operation_name="delivery.outbox",
            operation_name_hash=audit_hash(stored["sink_kind"]),
            request_hash=audit_hash(json.loads(stored["payload_json"])),
            created_at=stored["created_at"],
            settled_at=stored["settled_at"],
        )
    entries = list(facts(connection, run_id))
    endings = {
        (v["identity_hash"], v["version"]): (r, v)
        for r, v in entries
        if v["domain"] == "attempt" and v["state"] != "handoff"
    }
    for row, value in entries:
        attempt = value["domain"] == "attempt"
        if attempt and value["state"] != "handoff":
            continue
        end = endings.get((value["identity_hash"], value["version"])) if attempt else None
        yield RunOperationAuditV1(
            "delivery:"
            + value["identity_hash"]
            + ":"
            + str(value["version"])
            + (":attempt" if attempt else ":version"),
            "delivery",
            end[1]["state"] if end else ("unknown" if attempt else value["state"]),
            value["version"],
            value["source_hash"],
            row["event_id"],
            record_type="boundary" if attempt else "transition",
            operation_name="delivery.send" if attempt else "delivery." + value["operation"],
            operation_name_hash=value["sink_hash"],
            request_hash=value["request_hash"],
            created_at=row["created_at"],
            handed_off_at=row["created_at"] if attempt else None,
            settled_at=end[0]["created_at"] if end else None,
            result_hash=audit_hash(dict(end[0])) if end else None,
            error_code=end[1].get("error_code") if end else None,
            parent_operation_id="delivery:" + value["identity_hash"],
        )


def coverage(connection, run_id):
    entries = list(facts(connection, run_id))
    gaps = set()
    for row in connection.execute("SELECT * FROM delivery_outbox WHERE run_id=?", (run_id,)):
        row = dict(row)
        identity = _identity(row)
        versions = [
            v for _, v in entries if v["identity_hash"] == identity and v["domain"] == "version"
        ]
        if (
            [v["version"] for v in versions] != list(range(row["version"] + 1))
            or not versions
            or versions[-1]["source_hash"] != audit_hash(row)
        ):
            gaps.add("delivery_version_history_unverified")
        starts = {
            v["version"]: (r, v)
            for r, v in entries
            if v["identity_hash"] == identity
            and v["domain"] == "attempt"
            and v["state"] == "handoff"
        }
        for version in versions:
            if version["operation"] == "handoff":
                start = starts.get(version["version"])
                if start is None or start[1]["source_hash"] != version["source_hash"]:
                    gaps.add("delivery_physical_handoff_unverified")
        for _, value in entries:
            if (
                value["identity_hash"] == identity
                and value["domain"] == "attempt"
                and value["state"] != "handoff"
                and value["version"] not in starts
            ):
                gaps.add("delivery_physical_handoff_unverified")
        # A delivered CAS alone cannot attest that the SDK dispatcher called the sink.
        if row["state"] == "delivered" and not any(
            v["domain"] == "attempt"
            and v["state"] == "completed"
            and v["identity_hash"] == identity
            and v["version"] == row["version"] - 1
            for _, v in entries
        ):
            gaps.add("delivery_physical_handoff_unverified")
    return gaps
