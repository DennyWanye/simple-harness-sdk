# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""CAS helper for the fixed durable ReAct checkpoint namespace."""

from __future__ import annotations

import hashlib
from collections.abc import Callable

from simple_harness.contracts import RunId, canonical_json, thaw_json
from simple_harness.execution.context_authority import ContextRouteReceipt, ContextRouteState
from simple_harness.execution.uow import ExecutionLease, UnitOfWorkConflict, WorkflowCheckpoint

from .kernel import ReactCheckpointPort
from .termination import TerminationState


class DurableReactCheckpoint:
    def __init__(self, port: ReactCheckpointPort, *, clock: Callable[[], float]) -> None:
        self._port = port
        self._clock = clock

    def load_or_create(
        self,
        run_id: RunId,
        lease: ExecutionLease,
        *,
        initial_route_receipt: ContextRouteReceipt | None = None,
        initial_route_receipt_hash: str | None = None,
    ) -> tuple[TerminationState, int]:
        _validate_initial_route(run_id, initial_route_receipt, initial_route_receipt_hash)
        stored = self._port.read_react_checkpoint(run_id.value)
        if stored is not None:
            state = _state(stored)
            _require_route_match(state, initial_route_receipt, initial_route_receipt_hash)
            return state, stored.version
        state = TerminationState(
            self._clock(),
            route_state=(
                ContextRouteState.UNROUTED.value
                if initial_route_receipt is None
                else initial_route_receipt.route_state.value
            ),
            route_receipt=(
                None if initial_route_receipt is None else initial_route_receipt.to_json()
            ),
            route_receipt_hash=initial_route_receipt_hash,
            source_schema_version=6,
        )
        try:
            stored = self._write(run_id, lease, None, state)
        except UnitOfWorkConflict:
            stored = self._port.read_react_checkpoint(run_id.value)
            if stored is None:
                raise
            state = _state(stored)
            _require_route_match(state, initial_route_receipt, initial_route_receipt_hash)
        return state, stored.version

    def cas(
        self,
        run_id: RunId,
        lease: ExecutionLease,
        expected_version: int,
        state: TerminationState,
    ) -> tuple[TerminationState, int]:
        stored = self._write(run_id, lease, expected_version, state)
        return state, stored.version

    def _write(
        self,
        run_id: RunId,
        lease: ExecutionLease,
        expected_version: int | None,
        state: TerminationState,
    ) -> WorkflowCheckpoint:
        payload = state.to_json()
        digest = hashlib.sha256(canonical_json(payload).encode()).hexdigest()
        return self._port.cas_react_checkpoint(
            run_id=run_id.value,
            lease=lease,
            expected_version=expected_version,
            checkpoint=payload,
            checkpoint_hash=digest,
            now=self._clock(),
        )


def _state(value: WorkflowCheckpoint) -> TerminationState:
    payload = thaw_json(value.checkpoint)
    if not isinstance(payload, dict):
        raise TypeError("ReAct checkpoint payload must be an object")
    return TerminationState.from_json(payload)


def _validate_initial_route(
    run_id: RunId,
    receipt: ContextRouteReceipt | None,
    receipt_hash: str | None,
) -> None:
    if (receipt is None) != (receipt_hash is None):
        raise ValueError("initial Context route receipt/hash must be paired")
    if receipt is None:
        return
    if not isinstance(receipt_hash, str):
        raise TypeError("initial_route_receipt_hash must be a string or null")
    if not isinstance(receipt, ContextRouteReceipt):
        raise TypeError("initial_route_receipt must use ContextRouteReceipt")
    if receipt.run_id != run_id.value:
        raise ValueError("initial Context route belongs to another Run")
    if receipt.route_state is not ContextRouteState.ROUTED_TASK:
        raise ValueError("initial Context route must bind a TaskScope")
    if receipt.receipt_hash != receipt_hash:
        raise ValueError("initial Context route hash differs")


def _require_route_match(
    state: TerminationState,
    receipt: ContextRouteReceipt | None,
    receipt_hash: str | None,
) -> None:
    if receipt is None:
        return
    if (
        state.route_state != receipt.route_state.value
        or state.route_receipt != receipt.to_json()
        or state.route_receipt_hash != receipt_hash
    ):
        raise RuntimeError("ReAct checkpoint initial Context route differs from start snapshot")


__all__ = ("DurableReactCheckpoint",)
