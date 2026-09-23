# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""At-least-once notification delivery; original consumers retain work authority."""

from __future__ import annotations

import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from ..contracts.models import Event

from ..graph.notification_contracts import FollowupKind, FollowupV1
from ..storage.store import StoreConflict, StoreError
from ..storage.taskgraph_followups import BlockedFollowup, DurableFollowupReceipt, TaskGraphFollowupStore

FollowupHandler = Callable[[FollowupV1, str], Awaitable[DurableFollowupReceipt]]


class FollowupConsumerError(RuntimeError):
    """A classified consumer failure. Only this fixed code reaches durable storage."""

    def __init__(self, code: str) -> None:
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,95}", code):
            raise ValueError("consumer failure requires a named error code")
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True, kw_only=True)
class FollowupDelivery:
    message_id: str
    state: str


class TaskGraphFollowupPump:
    def __init__(
        self,
        notifications: TaskGraphFollowupStore,
        *,
        owner: str,
        reevaluate: FollowupHandler,
        converge: FollowupHandler,
        request_composition: FollowupHandler,
        clock_ms: Callable[[], int] | None = None,
        lease_ms: int = 30_000,
        require_execution_root: Callable[[], None] | None = None,
    ) -> None:
        self.notifications = notifications
        self.owner = owner
        # Fixed three consumers: never a user-supplied tool name or tool argument.
        self._handlers = {
            FollowupKind.REEVALUATE: reevaluate,
            FollowupKind.CONVERGE: converge,
            FollowupKind.REQUEST_COMPOSITION: request_composition,
        }
        self.clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self.lease_ms = lease_ms
        self._execution_root_check = require_execution_root

    def _require_execution_root(self) -> None:
        gate = getattr(self.notifications.store, "_assurance_root_gate", None)
        if gate is not None:
            gate.require_execution()
        if self._execution_root_check is not None:
            self._execution_root_check()

    async def pump_once(self) -> FollowupDelivery | None:
        self._require_execution_root()
        if self.notifications.store.connection.in_transaction:
            raise StoreError("TASKGRAPH_FOLLOWUP_PUMP_INSIDE_TRANSACTION")
        claim = self.notifications.claim_followup(
            self.owner, now_ms=self.clock_ms(), lease_ms=self.lease_ms
        )
        if claim is None:
            return None
        if isinstance(claim, BlockedFollowup):
            return FollowupDelivery(message_id=claim.message_id, state="BLOCKED")
        # The claim transaction is finished before entering any consumer. A cancellation
        # or process exit leaves a recoverable lease; it never asserts a worker has stopped.
        try:
            self._require_execution_root()
            receipt = await self._handlers[claim.message.kind](claim.message, claim.command_key)
            if not isinstance(receipt, DurableFollowupReceipt):
                raise FollowupConsumerError("FOLLOWUP_RECEIPT_INVALID")
            self.notifications.ack_followup(claim, receipt)
            return FollowupDelivery(message_id=claim.message_id, state="ACKED")
        except Exception as error:
            code = (
                error.code
                if isinstance(error, FollowupConsumerError)
                else "FOLLOWUP_HANDLER_FAILED"
            )
            try:
                state = self.notifications.retry_followup(
                    claim, error_code=code, now_ms=self.clock_ms()
                )
            except StoreConflict:
                state = "LEASE_LOST"
            return FollowupDelivery(message_id=claim.message_id, state=state)

    async def pump_taskgraph_followups(self, *, limit: int = 32) -> tuple[FollowupDelivery, ...]:
        """Bound each event-loop tick; blocked notifications remain visible in SQLite."""
        if type(limit) is not int or not 1 <= limit <= 256:
            raise ValueError("followup delivery limit must be between 1 and 256")
        deliveries = []
        for _ in range(limit):
            delivery = await self.pump_once()
            if delivery is None:
                break
            deliveries.append(delivery)
        return tuple(deliveries)


class TaskGraphEventConsumer:
    """Commit the source-event cursor and derived notifications together.

    The projector is fixed by SDK assembly and derives exact source references;
    it must raise for a recognized event with unreadable sources. It is never
    supplied by a model or Host payload and cannot perform external IO.
    """

    def __init__(self, notifications: TaskGraphFollowupStore, *, consumer_id: str,
                 projector: Callable[["Event"], tuple[FollowupV1, ...]]) -> None:
        from ..graph.notification_contracts import _text
        _text(consumer_id, "consumer_id")
        self.notifications = notifications
        self.consumer_id = consumer_id
        self.projector = projector

    def consume(self, mission_id: str, *, now_ms: int, batch_size: int = 128) -> int:
        from ..graph.notification_contracts import _integer
        from ..planning.htn.grounding import derive_id
        _integer(now_ms, "now_ms")
        if type(batch_size) is not int or not 1 <= batch_size <= 1024:
            raise ValueError("event batch must be between 1 and 1024")
        store = self.notifications.store
        key = derive_id("taskgraph-exec-v2-event-cursor", self.consumer_id, mission_id)
        with store.transaction() as db:
            if db.execute("SELECT 1 FROM taskgraph_policy_bindings WHERE mission_id=?", (mission_id,)).fetchone() is None:
                raise StoreError("TASKGRAPH_POLICY_UNAVAILABLE")
            try:
                saved = store.get_scheduler_state(key)
            except (ValueError, TypeError) as error:
                raise StoreError("TASKGRAPH_EVENT_CURSOR_CORRUPT") from error
            if saved is None:
                through = 0
            elif (set(saved) != {"mission_id", "consumer_id", "through_seq"}
                  or saved["mission_id"] != mission_id or saved["consumer_id"] != self.consumer_id):
                raise StoreError("TASKGRAPH_EVENT_CURSOR_CORRUPT")
            else:
                through = _integer(saved["through_seq"], "through_seq")
            events = store.list_events(mission_id, after_seq=through, limit=batch_size)
            for event in events:
                if event.seq is None or event.seq <= through:
                    raise StoreError("TASKGRAPH_EVENT_ORDER_CORRUPT")
                for followup in self.projector(event):
                    if followup.mission_id != mission_id or followup.source_event_id != event.id:
                        raise StoreError("TASKGRAPH_EVENT_PROJECTION_SOURCE_MISMATCH")
                    self.notifications.append_followup(followup, now_ms=now_ms)
                through = event.seq
            if events:
                store.put_scheduler_state(key, {"mission_id": mission_id, "consumer_id": self.consumer_id,
                                                "through_seq": through})
            return len(events)
