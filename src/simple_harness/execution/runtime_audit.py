"""SDK operation boundaries before canonical Provider/effect handoff ledgers."""

from __future__ import annotations

from contextlib import contextmanager
from uuid import uuid4

from .audit import audit_error_code, audit_hash

RUNTIME_BOUNDARIES = frozenset(
    {
        "runtime.driver",
        "runtime.preflight",
        "provider.prepare",
        "context.prepare",
        "context.verify",
        "tool.batch",
        "tool.envelope",
        "tool.proposal",
        "tool.preflight",
        "tool.route_gate",
        "context.no_recall",
        "context.apply",
    }
)


@contextmanager
def runtime_operation(store, name, *, lease, clock, identity, contract="sdk.core.v2"):
    """An actual call interval, not a declaration that later execution is covered."""
    writer = getattr(store, "record_runtime_operation", None)
    if writer is None:
        yield {}
        return
    operation_id = uuid4().hex
    started_at = clock()
    receipt = {}

    def record(state, error_code=None):
        writer(
            name=name,
            operation_id=operation_id,
            state=state,
            lease=lease,
            identity_hash=audit_hash(identity),
            receipt_hash=audit_hash(receipt),
            contract=contract,
            started_at=started_at,
            now=clock(),
            error_code=error_code,
        )

    record("started")
    try:
        yield receipt
    except BaseException as original:
        try:
            state = "failed" if isinstance(original, Exception) else "interrupted"
            record(
                state,
                audit_error_code(getattr(original, "code", None)) or "runtime_boundary_" + state,
            )
        except Exception as recording_error:
            # Lease loss may prevent settlement. Keep the original dispatch
            # classification; the unmatched durable start remains unverified.
            raise original from recording_error
        raise
    else:
        state = receipt.get("audit_outcome", "completed")
        record(state, "runtime_boundary_rejected" if state == "rejected" else None)
