# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""Original-call accounting receipts in the existing witnessed event journal."""

from __future__ import annotations

import json
import math
from typing import TYPE_CHECKING

from simple_harness.execution.provider_accounting import (
    ACCOUNTING_KIND,
    accounting_event_id,
    accounting_payload,
    effective_accounting,
    needs_accounting,
)
from simple_harness.execution.provider_invocations import ProviderInvocationRecord
from simple_harness.execution.uow import UnitOfWorkConflict, UnitOfWorkNotFound
from simple_harness.providers import ProviderAccountingObservation


class ProviderAccountingMixin:
    __slots__ = ()

    if TYPE_CHECKING:
        from .database import Database

        database: Database

        def read_provider_invocation(
            self, invocation_id: str
        ) -> ProviderInvocationRecord | None: ...
        def _insert_event(self, connection, *, event_id, run_id, kind, payload, now) -> None: ...

    def read_provider_accounting_receipt(self, invocation_id: str):
        original = self.read_provider_invocation(invocation_id)
        if original is None:
            raise UnitOfWorkNotFound(invocation_id)
        row = self.database.connection.execute(
            "SELECT run_id,kind,payload_json FROM run_events WHERE event_id=?",
            (accounting_event_id(original),),
        ).fetchone()
        if row is None:
            return None
        if row["run_id"] != original.run_id.value or row["kind"] != ACCOUNTING_KIND:
            raise UnitOfWorkConflict("provider accounting event identity collision")
        try:
            payload = json.loads(row["payload_json"])
            effective_accounting(original, payload)
        except (TypeError, ValueError) as exc:
            raise UnitOfWorkConflict("provider accounting event payload is invalid") from exc
        return payload

    def read_effective_provider_invocation(
        self, invocation_id: str
    ) -> ProviderInvocationRecord | None:
        original = self.read_provider_invocation(invocation_id)
        if original is None:
            return None
        payload = self.read_provider_accounting_receipt(invocation_id)
        return original if payload is None else effective_accounting(original, payload)

    def list_pending_provider_accounting(self) -> tuple[ProviderInvocationRecord, ...]:
        rows = self.database.connection.execute(
            "SELECT invocation_id FROM provider_invocations WHERE state IN ('succeeded','failed')"
            " AND handoff_attempt>=1 ORDER BY claimed_at,invocation_id"
        ).fetchall()
        pending = []
        for row in rows:
            record = self.read_provider_invocation(row["invocation_id"])
            assert record is not None
            # Validate any existing receipt against raw authority even on a
            # read; a colliding/corrupt event is not a reason to silently skip.
            receipt = self.read_provider_accounting_receipt(record.invocation_id)
            if receipt is None and needs_accounting(record):
                pending.append(record)
        return tuple(pending)

    def record_provider_accounting(
        self,
        record: ProviderInvocationRecord,
        *,
        observation: ProviderAccountingObservation,
        now: float,
        fault=None,
    ):
        if isinstance(now, bool) or not math.isfinite(now) or now < 0:
            raise ValueError("accounting receipt time must be finite and nonnegative")
        payload = accounting_payload(record, observation)
        with self.database.transaction() as connection:
            current = self.read_provider_invocation(record.invocation_id)
            if current is None:
                raise UnitOfWorkNotFound(record.invocation_id)
            if current != record:
                raise UnitOfWorkConflict("provider accounting original record changed")
            existing = self.read_provider_accounting_receipt(record.invocation_id)
            if existing is not None:
                if existing != payload:
                    raise UnitOfWorkConflict("provider accounting immutable receipt conflict")
                return existing
            if fault:
                fault("provider_accounting.before_write")
            self._insert_event(
                connection,
                event_id=accounting_event_id(record),
                run_id=record.run_id.value,
                kind=ACCOUNTING_KIND,
                payload=payload,
                now=now,
            )
            if fault:
                fault("provider_accounting.after_write")
        return payload
