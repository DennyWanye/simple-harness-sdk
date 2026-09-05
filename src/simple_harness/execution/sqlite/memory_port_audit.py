"""Harness-owned committed-turn calls; no claims about Memory internal processing."""

import json
from dataclasses import asdict

from simple_harness.contracts import canonical_json
from simple_harness.execution.audit import (
    RunAuditUnavailable,
    RunOperationAuditV1,
    audit_error_code,
    audit_hash,
)

KIND = "audit.memory_outbox.v1"


def _row(connection, identity):
    return dict(
        connection.execute("SELECT * FROM memory_outbox WHERE intent_id=?", (identity,)).fetchone()
    )


def _identity(row):
    return audit_hash([row["intent_id"], row["run_id"], row["payload_hash"], row["created_at"]])


def facts(connection, run_id):
    for row in connection.execute(
        "SELECT * FROM run_events WHERE run_id=? AND kind=? ORDER BY durable_seq", (run_id, KIND)
    ):
        value = json.loads(row["payload_json"])
        if row["event_id"] != "audit-memory:" + audit_hash(value):
            raise RunAuditUnavailable("memory_outbox_fact_invalid")
        yield row, value


def _append(connection, row, value, now):
    connection.execute(
        "INSERT INTO run_events(event_id,run_id,durable_seq,kind,payload_json,created_at) "
        "SELECT ?,?,COALESCE(MAX(durable_seq),0)+1,?,?,? FROM run_events WHERE run_id=?",
        (
            "audit-memory:" + audit_hash(value),
            row["run_id"],
            KIND,
            canonical_json(value),
            now,
            row["run_id"],
        ),
    )


def record(connection, identity, *, operation, now, claim=None, receipt=None):
    row = _row(connection, identity)
    owner = row["claim_owner"] if claim is None else claim.claim_owner
    value = dict(
        identity_hash=_identity(row),
        operation=operation,
        state=row["state"],
        claim_epoch=row["claim_epoch"],
        attempt_count=row["attempt_count"],
        owner_hash=None if owner is None else audit_hash(owner),
        source_hash=audit_hash(row),
        request_hash=row["payload_hash"],
        error_code=audit_error_code(row["error_code"]),
        error_code_hash=None if row["error_code"] is None else audit_hash(row["error_code"]),
    )
    if receipt is not None:
        value["receipt_hash"] = audit_hash(asdict(receipt))
        value["receipt_status"] = receipt.status.value
    if operation in {"released", "settled"}:
        starts = [
            v
            for _, v in facts(connection, row["run_id"])
            if v["identity_hash"] == value["identity_hash"]
            and v["operation"] == "handoff"
            and v["claim_epoch"] == row["claim_epoch"]
            and v["owner_hash"] == value["owner_hash"]
        ]
        value["handoff_hash"] = audit_hash(starts[0]) if starts else None
    _append(connection, row, value, now)


def begin(connection, claim, now):
    from simple_harness.execution.uow import UnitOfWorkConflict

    row = _row(connection, claim.intent_id)
    if (
        row["state"] != "claimed"
        or row["claim_epoch"] != claim.claim_epoch
        or row["claim_owner"] != claim.claim_owner
        or row["claim_expires_at"] <= now
    ):
        raise UnitOfWorkConflict("memory port claim is stale")
    record(connection, claim.intent_id, operation="handoff", now=now, claim=claim)


def operations(connection, run_id):
    entries = list(facts(connection, run_id))
    introduced = {v["identity_hash"] for _, v in entries if v["operation"] == "created"}
    observed = set(introduced)
    for row, value in entries:
        if value["identity_hash"] not in observed:
            observed.add(value["identity_hash"])
            yield RunOperationAuditV1(
                "memory_port:" + value["identity_hash"],
                "memory_port",
                "legacy_observed",
                row["durable_seq"],
                value["source_hash"],
                row["event_id"],
                record_type="receipt",
                operation_name="memory.outbox.observed",
                created_at=row["created_at"],
                request_hash=value["request_hash"],
            )
    for row in connection.execute("SELECT * FROM memory_outbox WHERE run_id=?", (run_id,)):
        value = dict(row)
        if _identity(value) not in observed:
            yield RunOperationAuditV1(
                "memory_port:" + _identity(value),
                "memory_port",
                value["state"],
                value["claim_epoch"],
                audit_hash(value),
                "memory:" + audit_hash(value["intent_id"]),
                operation_name="memory.outbox.observed",
                created_at=value["created_at"],
                request_hash=value["payload_hash"],
                claim_epoch=value["claim_epoch"],
            )
    endings = {
        (v["identity_hash"], v["claim_epoch"]): (r, v)
        for r, v in entries
        if v["operation"] in {"settled", "released"}
    }
    for row, value in entries:
        attempt = value["operation"] == "handoff"
        end = endings.get((value["identity_hash"], value["claim_epoch"])) if attempt else None
        if end and end[1].get("handoff_hash") != audit_hash(value):
            raise RunAuditUnavailable("memory_outbox_attempt_binding_invalid")
        state = (
            (end[1].get("receipt_status", "unknown") if end else "unknown")
            if attempt
            else value["state"]
        )
        root = "memory_port:" + value["identity_hash"]
        yield RunOperationAuditV1(
            root if value["operation"] == "created" else root + ":" + str(row["durable_seq"]),
            "memory_port",
            state,
            row["durable_seq"],
            value["source_hash"],
            row["event_id"],
            record_type="boundary" if attempt else "receipt",
            operation_name="memory.record_committed_turn"
            if attempt
            else "memory.outbox." + value["operation"],
            claim_epoch=value["claim_epoch"],
            attempt_count=value["attempt_count"],
            owner_ref_hash=value["owner_hash"],
            request_hash=value["request_hash"],
            created_at=row["created_at"],
            handed_off_at=row["created_at"] if attempt else None,
            settled_at=end[0]["created_at"] if end else None,
            result_hash=end[1].get("receipt_hash") if end else None,
            error_code=end[1].get("error_code") if end else value.get("error_code"),
            error_code_hash=end[1].get("error_code_hash") if end else value.get("error_code_hash"),
            parent_operation_id=None if value["operation"] == "created" else root,
        )


def coverage(connection, run_id):
    entries = list(facts(connection, run_id))
    groups = {}
    for _, value in entries:
        groups.setdefault(value["identity_hash"], []).append(value)
    heads = {
        _identity(dict(row)): dict(row)
        for row in connection.execute("SELECT * FROM memory_outbox WHERE run_id=?", (run_id,))
    }
    gaps = set()
    for identity in set(groups) | set(heads):
        values = groups.get(identity, [])
        head = heads.get(identity)
        if not values or values[0]["operation"] != "created":
            gaps.add("memory_outbox_introduction_unverified")
        if not values:
            continue
        last = values[-1]
        if head is None:
            if last["operation"] != "cleaned":
                gaps.add("memory_outbox_cleanup_unverified")
        elif last["source_hash"] != audit_hash(head):
            gaps.add("memory_outbox_current_head_unverified")
        epochs = [v["claim_epoch"] for v in values if v["operation"] == "claimed"]
        if epochs != list(range(1, last["claim_epoch"] + 1)):
            gaps.add("memory_outbox_claim_history_unverified")
        for epoch in epochs:
            if not any(v["operation"] == "handoff" and v["claim_epoch"] == epoch for v in values):
                gaps.add("memory_outbox_physical_interval_unverified")
        if last["state"] in {"applied", "dead_letter", "retry_wait"} and not any(
            v["operation"] in {"released", "settled"} and v["source_hash"] == last["source_hash"]
            for v in values
        ):
            gaps.add("memory_outbox_settlement_unverified")
        for value in values:
            if value["operation"] in {"released", "settled"}:
                if not any(
                    v["operation"] == "handoff" and audit_hash(v) == value.get("handoff_hash")
                    for v in values
                ):
                    gaps.add("memory_outbox_physical_interval_unverified")
    return gaps
