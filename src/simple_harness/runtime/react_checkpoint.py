# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""CAS helper for the fixed durable ReAct checkpoint namespace."""

from __future__ import annotations

import hashlib
from collections.abc import Callable

from simple_harness.contracts import RunId, canonical_json, thaw_json
from simple_harness.execution.uow import ExecutionLease, WorkflowCheckpoint

from .kernel import ReactCheckpointPort
from .termination import TerminationState


class DurableReactCheckpoint:
    def __init__(self, port: ReactCheckpointPort, *, clock: Callable[[], float]) -> None:
        self._port = port
        self._clock = clock

    def load_or_create(
        self, run_id: RunId, lease: ExecutionLease
    ) -> tuple[TerminationState, int]:
        stored = self._port.read_react_checkpoint(run_id.value)
        if stored is not None:
            return _state(stored), stored.version
        state = TerminationState(self._clock())
        stored = self._write(run_id, lease, None, state)
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


__all__ = ("DurableReactCheckpoint",)
