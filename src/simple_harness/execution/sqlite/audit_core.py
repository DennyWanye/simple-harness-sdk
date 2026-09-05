"""Explicit SDK control/child/workflow canonical sources. No diagnostic inference."""

from simple_harness.execution.audit import RunOperationAuditV1, audit_hash, audit_reference

# Table, immutable/composite identity, actual owning Run columns, public domain.
CORE_SOURCES = (
    ("continuation_progress_receipts", ("receipt_id",), ("run_id",), "control"),
    ("wait_activation_receipts", ("receipt_id",), ("run_id",), "control"),
    ("runtime_start_receipts", ("run_id",), ("run_id",), "control"),
    ("runtime_start_dispatch_claims", ("claim_id",), ("run_id",), "control"),
    ("conversation_outputs", ("run_id",), ("run_id",), "control"),
    ("context_preparation_staging", ("stage_id",), ("consumed_run_id",), "context"),
    ("profile_launch_tickets", ("ticket_id",), ("parent_run_id", "child_run_id"), "child"),
    ("child_commands", ("command_id",), ("parent_run_id", "child_run_id"), "child"),
    ("run_links", ("parent_run_id", "child_run_id"), ("parent_run_id", "child_run_id"), "child"),
    ("child_terminal_receipts", ("receipt_id",), ("child_run_id",), "child"),
    ("child_signals", ("signal_id",), ("parent_run_id", "child_run_id"), "child"),
    ("child_signal_ack_receipts", ("receipt_id",), ("parent_run_id",), "child"),
    (
        "workflow_launch_ticket_receipts",
        ("ticket_receipt_id",),
        ("resolved_run_id", "parent_run_id"),
        "workflow",
    ),
    ("workflow_operation_receipts", ("operation_id",), ("run_id",), "workflow"),
    ("workflow_native_operations", ("operation_id",), ("run_id",), "workflow"),
    ("workflow_checkpoint_effect_links", ("checkpoint_id", "effect_id"), ("run_id",), "workflow"),
    ("workflow_decision_consumptions", ("decision_id", "checkpoint_id"), ("run_id",), "workflow"),
    ("workflow_start_admissions", ("request_id",), ("run_id",), "workflow"),
    ("workflow_resume_admissions", ("receipt_id",), ("run_id",), "workflow"),
    ("workflow_cancel_receipts", ("cancel_id",), ("run_id",), "workflow"),
    ("workflow_recovery_receipts", ("receipt_id",), ("run_id",), "workflow"),
    ("workflow_recovery_claims", ("blocker_id",), ("run_id",), "workflow"),
    ("workflow_fork_receipts", ("fork_id",), ("source_run_id", "target_run_id"), "workflow"),
    ("workflow_spawn_continuations", ("operation_id",), ("parent_run_id",), "workflow"),
    (
        "workflow_spawn_ready_activations",
        ("activation_receipt_id",),
        ("parent_run_id",),
        "workflow",
    ),
    (
        "workflow_spawn_child_wait_receipts",
        ("parent_wait_receipt_id",),
        ("parent_run_id", "child_run_id"),
        "workflow",
    ),
    (
        "workflow_spawn_completion_receipts",
        ("completion_receipt_id",),
        ("parent_run_id",),
        "workflow",
    ),
    ("workflow_terminal_fence_receipts", ("receipt_id",), ("run_id",), "workflow"),
    ("workflow_terminal_receipts", ("receipt_id",), ("run_id",), "workflow"),
)
STATES = frozenset(
    {
        "created",
        "committed",
        "claimed",
        "pending",
        "scheduled",
        "acked",
        "failed",
        "cancelled",
        "issued",
        "waiting",
        "completed",
        "resolved",
        "queued",
        "running",
        "prepared",
        "ready",
        "consumed",
        "quarantined",
        "abandoned",
        "staged",
        "preparing",
        "started",
        "succeeded",
        "unknown",
        "partial",
        "denied",
        "allowed",
        "expired",
    }
)


def core_rows(connection, run_id, *, limit):
    for table, keys, owners, kind in CORE_SOURCES:
        where = " OR ".join(f"{column}=?" for column in owners)
        rows = connection.execute(
            f"SELECT * FROM {table} WHERE {where} ORDER BY {','.join(keys)} LIMIT ?",
            (*([run_id] * len(owners)), limit),
        )
        for row in rows:
            yield table, keys, kind, dict(row)
    # No Run column here: association is the canonical spawn operation, not time/ID guessing.
    for row in connection.execute(
        "SELECT r.* FROM workflow_spawn_continuation_ready r "
        "JOIN workflow_spawn_continuations c ON c.operation_id=r.operation_id "
        "WHERE c.parent_run_id=? ORDER BY r.ready_receipt_id LIMIT ?",
        (run_id, limit),
    ):
        yield "workflow_spawn_continuation_ready", ("ready_receipt_id",), "workflow", dict(row)


def core_operation(table, keys, kind, row):
    identity = table + ":" + audit_hash([row[k] for k in keys])
    state = row.get("state", row.get("phase", row.get("terminal_state", "recorded")))
    # Full original bytes are hashed, never exported. BLOBs use explicit hex encoding.
    source = {k: {"bytes_hex": v.hex()} if isinstance(v, bytes) else v for k, v in row.items()}
    return RunOperationAuditV1(
        kind + ":" + identity,
        kind,
        state if state in STATES else "recorded",
        row.get("version", 0),
        audit_hash(source),
        identity,
        record_type="receipt",
        operation_name=table,
        created_at=row.get("created_at", row.get("issued_at")),
        runtime_epoch=row.get("runtime_lease_epoch"),
        claim_epoch=row.get("claim_epoch"),
        attempt_count=row.get("attempt_count"),
        effect_id=row.get("effect_id"),
        request_hash=row.get("request_fingerprint", row.get("input_hash")),
        result_hash=row.get("outcome_hash", row.get("output_hash")),
        related_run_refs=tuple(
            sorted(
                {
                    audit_reference("run", row[key])
                    for key in (
                        "parent_run_id",
                        "child_run_id",
                        "source_run_id",
                        "target_run_id",
                    )
                    if row.get(key) is not None
                }
            )
        ),
    )
