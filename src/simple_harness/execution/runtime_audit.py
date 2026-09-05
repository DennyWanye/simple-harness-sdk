"""SDK operation boundaries before canonical Provider/effect handoff ledgers."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
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

_CURRENT_OPERATION = ContextVar("sdk_runtime_audit_operation", default=None)


class OperationReceipt(dict):
    def __init__(self, operation_id=None):
        super().__init__()
        self.operation_id = operation_id
        self.child_operation_ids = []


def current_operation_binding(run_id):
    active = _CURRENT_OPERATION.get()
    if active is None or active[0] != run_id:
        return None
    return dict(operation_id=active[3], runtime_epoch=active[2], owner_hash=audit_hash(active[1]))


@contextmanager
def runtime_operation(
    store, name, *, lease, clock, identity, contract="sdk.core.v2", parent_operation_id=None
):
    """An actual call interval, not a declaration that later execution is covered."""
    writer = getattr(store, "record_runtime_operation", None)
    if writer is None:
        yield OperationReceipt()
        return
    operation_id = uuid4().hex
    started_at = clock()
    receipt = OperationReceipt(operation_id)
    owner = (lease.run_id, lease.owner_id, lease.epoch)
    active = _CURRENT_OPERATION.get()
    if parent_operation_id is None and active is not None and active[:3] == owner:
        parent_operation_id = active[3]

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
            parent_operation_id=parent_operation_id,
            child_operation_ids=list(receipt.child_operation_ids),
        )

    record("started")
    if active is not None and active[:3] == owner:
        active[4].child_operation_ids.append(operation_id)
    token = _CURRENT_OPERATION.set((*owner, operation_id, receipt))
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
    finally:
        _CURRENT_OPERATION.reset(token)
