# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Permanent root terminal commits owned by the RunKernel."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from simple_harness.contracts import HarnessError, JsonValue
from simple_harness.execution.delivery import DeliverySpec, TerminalCommitResult
from simple_harness.execution.fences import RunFenceLease
from simple_harness.execution.uow import ExecutionUnitOfWork, RunRecord, RunState


class ToolCatalogStale(HarnessError):
    def __init__(self) -> None:
        super().__init__(
            "tool_catalog_stale",
            "The durable Tool catalog snapshot is no longer available.",
            retryable=False,
        )


class TerminalCoordinator:
    def __init__(self, uow: ExecutionUnitOfWork) -> None:
        self._uow = uow

    def commit(
        self,
        run: RunRecord,
        *,
        state: RunState,
        payload: Mapping[str, JsonValue],
        deliveries: Sequence[DeliverySpec],
        fence: RunFenceLease,
        now: float,
    ) -> TerminalCommitResult:
        return self._uow.commit_root_terminal_with_deliveries(
            run_id=run.run_id,
            expected_version=run.version,
            terminal_state=state,
            event_id=f"{run.run_id}:terminal:{state.value}",
            terminal_payload=payload,
            deliveries=deliveries,
            fence=fence,
            terminal_fence_receipt_ref=(
                f"runtime-fence:{fence.owner_id}:{fence.epoch}"
            ),
            now=now,
        )


__all__ = ("TerminalCoordinator", "ToolCatalogStale")
